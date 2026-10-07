"""
Lambda handler: Deep Insight Job Complete

Three triggers:

1. S3 PUT of job_status.json (runtimes that receive job_id in the payload):
     s3://{bucket}/deep-insight/fargate_sessions/{session_id}/output/job_status.json
   The runtime writes it on every exit path -- success, exception, client
   disconnect -- so the job settles even if the browser stream was cut off.
   1. Read job_status.json (job_id, session_id, Success/Failed, error)
   2. Get the DynamoDB record by job_id
   3. Idempotency guard: skip if already Success
   4. Read token_usage.json (if present) and list artifacts
   5. Update the record with the final status, stats and trace path
   6. Publish SNS notification if the status changed

2. S3 PUT of token_usage.json (runtimes that predate job_status.json):
     s3://{bucket}/deep-insight/fargate_sessions/{session_id}/output/token_usage.json
   1. Skip if the file carries job_id (trigger 1 reports that job)
   2. Find the DynamoDB record via SessionIdIndex GSI
   3. Idempotency guard: skip if already Success
   4. Update the record with Success status and stats, publish SNS

3. EventBridge schedule: jobs still in Start after STALE_JOB_MINUTES become
   Failed. Covers a runtime that died without writing job_status.json. A later
   job_status.json Success still overrides it.

Environment variables:
  DYNAMODB_TABLE_NAME: DynamoDB table name (e.g., deep-insight-jobs)
  SNS_TOPIC_ARN: SNS topic ARN for notifications
  STALE_JOB_MINUTES: minutes before a Start job is marked Failed (default 120)
"""

import json
import logging
import os
import time
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
STALE_JOB_MINUTES = int(os.environ.get("STALE_JOB_MINUTES", "120"))

SESSIONS_PREFIX = "deep-insight/fargate_sessions/"


def handler(event, context):
    """Process S3 PUT events, or sweep stale jobs on the EventBridge schedule."""
    if event.get("source") == "aws.events":
        _sweep_stale_jobs()
        return {"statusCode": 200, "body": "OK"}

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        logger.info(f"Processing: s3://{bucket}/{key}")

        try:
            if key.endswith("/job_status.json"):
                _process_job_status(bucket, key)
            else:
                _process_job_complete(bucket, key)
        except Exception as e:
            logger.error(f"Failed to process {key}: {e}", exc_info=True)

    return {"statusCode": 200, "body": "OK"}


def _get_table():
    return boto3.resource("dynamodb").Table(DYNAMODB_TABLE_NAME)


# ---------- Trigger 1: job_status.json ----------


def _process_job_status(bucket: str, key: str):
    """Process a single job_status.json upload event."""
    s3 = boto3.client("s3")
    table = _get_table()

    # Step 1: Read job_status.json
    status_data = _read_json(s3, bucket, key)
    job_id = status_data.get("job_id")
    if not job_id:
        logger.info(f"No job_id in {key} (not a Web UI job) — skipping")
        return

    session_id = status_data.get("session_id", "")
    new_status = "Success" if status_data.get("status") == "Success" else "Failed"

    # Step 2: Get the DynamoDB record by job_id
    job_record = table.get_item(Key={"job_id": job_id}).get("Item")
    if not job_record:
        logger.warning(f"No DynamoDB record found for job_id={job_id}")
        return

    # Step 3: Idempotency guard
    previous_status = job_record.get("status")
    if previous_status == "Success":
        logger.info(f"Job {job_id} already marked Success — skipping")
        return

    # Step 4: Token usage (absent if no model call finished) and artifacts
    output_prefix = key.rsplit("/", 1)[0] + "/"
    token_data = _read_json(s3, bucket, output_prefix + "token_usage.json")
    artifacts = _list_artifacts(s3, bucket, session_id)
    stats = _job_stats(token_data, artifacts, session_id)

    # Step 5: Update DynamoDB record
    ended_at = int(status_data.get("ended_at") or time.time())
    started_at = int(job_record.get("started_at", ended_at))
    fields = {
        "status": new_status,
        "ended_at": ended_at,
        "elapsed_seconds": max(ended_at - started_at, 0),
        "session_id": session_id,
        "trace_path": status_data.get("trace_path", ""),
        **stats,
    }
    remove = []
    if new_status == "Failed":
        fields["error_message"] = status_data.get("error") or "Run failed"
    else:
        remove.append("error_message")  # from an earlier stale-job sweep
    _update_job(table, job_id, fields, remove)
    logger.info(f"DynamoDB updated: job_id={job_id}, status={new_status}, previous={previous_status}")

    # Step 6: Notify only on a status change. The web server already notified
    # failures it saw itself (and recorded them as Failed).
    if previous_status == new_status:
        return
    if new_status == "Success":
        _publish_notification(job_id, job_record, fields["elapsed_seconds"], stats["total_tokens"],
                              stats["cache_hit_rate"], stats["report_filename"])
    else:
        _publish_failure_notification(job_id, job_record, fields["error_message"])


# ---------- Trigger 2: token_usage.json (legacy runtimes) ----------


def _process_job_complete(bucket: str, key: str):
    """Process a single token_usage.json upload event."""

    # Step 1: Extract session_id from S3 key path
    # Key format: deep-insight/fargate_sessions/{session_id}/output/token_usage.json
    parts = key.split("/")
    try:
        session_idx = parts.index("fargate_sessions") + 1
        session_id = parts[session_idx]
    except (ValueError, IndexError):
        logger.error(f"Cannot extract session_id from key: {key}")
        return

    logger.info(f"Session ID: {session_id}")

    s3 = boto3.client("s3")
    table = _get_table()

    # Step 2: Read token_usage.json
    token_data = _read_json(s3, bucket, key)
    if not token_data:
        return

    # Runtimes that write job_id also write job_status.json, which reports the
    # job (including failures). Handling both would process the job twice.
    if "job_id" in token_data:
        logger.info(f"token_usage.json carries job_id — reported by job_status.json, skipping")
        return

    # Step 3: List artifacts
    artifacts = _list_artifacts(s3, bucket, session_id)

    # Step 4: Find DynamoDB record via SessionIdIndex GSI
    job_record = _find_job_by_session_id(table, session_id)
    if not job_record:
        logger.warning(f"No DynamoDB record found for session_id={session_id}")
        return

    job_id = job_record["job_id"]
    logger.info(f"Found job_id={job_id} for session_id={session_id}")

    # Step 5: Idempotency guard
    if job_record.get("status") == "Success":
        logger.info(f"Job {job_id} already marked Success — skipping")
        return

    # Step 6: Update DynamoDB record
    ended_at = int(time.time())
    started_at = int(job_record.get("started_at", ended_at))
    elapsed_seconds = ended_at - started_at
    stats = _job_stats(token_data, artifacts, session_id)

    _update_job(table, job_id, {
        "status": "Success",
        "ended_at": ended_at,
        "elapsed_seconds": elapsed_seconds,
        **stats,
    })
    logger.info(f"DynamoDB updated: job_id={job_id}, status=Success, elapsed={elapsed_seconds}s")

    # Step 7: Publish SNS notification
    _publish_notification(job_id, job_record, elapsed_seconds, stats["total_tokens"],
                          stats["cache_hit_rate"], stats["report_filename"])


# ---------- Trigger 3: stale job sweep ----------


def _sweep_stale_jobs():
    """Mark jobs still in Start after STALE_JOB_MINUTES as Failed."""
    table = _get_table()
    now = int(time.time())
    cutoff = now - STALE_JOB_MINUTES * 60
    error = f"No completion signal within {STALE_JOB_MINUTES} minutes (runtime stopped without reporting)"

    query = {
        "IndexName": "StatusStartedIndex",
        "KeyConditionExpression": "#s = :start AND started_at < :cutoff",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":start": "Start", ":cutoff": cutoff},
    }
    stale = []
    while True:
        response = table.query(**query)
        stale.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        query["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    marked = 0
    for job in stale:
        job_id = job["job_id"]
        try:
            # Condition: a status report may land between the query and this write
            table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #s = :failed, ended_at = :ended, error_message = :err",
                ConditionExpression="#s = :start",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":failed": "Failed", ":ended": now, ":err": error, ":start": "Start"},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            continue
        marked += 1
        _publish_failure_notification(job_id, job, error)

    logger.info(f"Stale job sweep: {len(stale)} found, {marked} marked Failed (cutoff {STALE_JOB_MINUTES} min)")


# ---------- Helpers ----------


def _job_stats(token_data: dict, artifacts: list, session_id: str) -> dict:
    """DynamoDB fields derived from token_usage.json and the artifact list."""
    summary = token_data.get("summary", {})
    input_tokens = summary.get("total_input_tokens", 0)
    cache_read = summary.get("cache_read_input_tokens", 0)
    cache_hit_rate = round((cache_read / input_tokens * 100), 1) if input_tokens > 0 else 0

    # Find report file (prefer .docx over .txt)
    report_filename = ""
    txt_fallback = ""
    for f in artifacts:
        if f.endswith(".docx"):
            report_filename = f
            break
        if f.endswith(".txt") and not txt_fallback:
            txt_fallback = f
    if not report_filename:
        report_filename = txt_fallback
    report_path = f"{SESSIONS_PREFIX}{session_id}/artifacts/{report_filename}" if report_filename else ""

    return {
        "total_tokens": summary.get("total_tokens", 0),
        "input_tokens": input_tokens,
        "output_tokens": summary.get("total_output_tokens", 0),
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": summary.get("cache_write_input_tokens", 0),
        "cache_hit_rate": int(cache_hit_rate),
        "report_path": report_path,
        "report_filename": report_filename,
        "output_files": artifacts,
    }


def _update_job(table, job_id: str, fields: dict, remove: list = ()):
    """SET every field (and REMOVE the listed ones) on the job record."""
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    if remove:
        names.update({f"#{k}": k for k in remove})
        expression += " REMOVE " + ", ".join(f"#{k}" for k in remove)
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _read_json(s3, bucket: str, key: str) -> dict:
    """Read and parse a JSON object from S3. Returns {} if missing or invalid."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as e:
        logger.warning(f"Could not read s3://{bucket}/{key}: {e}")
        return {}


def _list_artifacts(s3, bucket: str, session_id: str) -> list:
    """List artifact filenames for a session."""
    prefix = f"{SESSIONS_PREFIX}{session_id}/artifacts/"
    try:
        filenames = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"].removeprefix(prefix)
                if name:
                    filenames.append(name)
        return filenames
    except Exception as e:
        logger.error(f"Failed to list artifacts: {e}")
        return []


def _find_job_by_session_id(table, session_id: str) -> dict:
    """Query SessionIdIndex GSI to find the job record by session_id."""
    try:
        response = table.query(
            IndexName="SessionIdIndex",
            KeyConditionExpression="session_id = :sid",
            ExpressionAttributeValues={":sid": session_id},
        )
        items = response.get("Items", [])
        if not items:
            return {}

        # GSI is KEYS_ONLY — need to get the full record
        job_id = items[0]["job_id"]
        result = table.get_item(Key={"job_id": job_id})
        return result.get("Item", {})
    except Exception as e:
        logger.error(f"Failed to query SessionIdIndex: {e}")
        return {}


def _publish(subject: str, message: str, job_id: str):
    if not SNS_TOPIC_ARN:
        return
    try:
        boto3.client("sns").publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        logger.info(f"SNS notification sent for job_id={job_id}")
    except Exception as e:
        logger.error(f"Failed to publish SNS notification: {e}")


def _publish_notification(job_id: str, job_record: dict, elapsed_seconds: int,
                          total_tokens: int, cache_hit_rate: float, report_filename: str):
    """Publish job completion notification to SNS topic."""
    user_query = job_record.get("user_query", "")[:100]
    message = (
        "Deep Insight Job Completed\n"
        "\n"
        f"Job ID: {job_id}\n"
        f"Status: Success\n"
        f"Query: {user_query}\n"
        f"Duration: {elapsed_seconds}s\n"
        f"Tokens: {total_tokens:,}\n"
        f"Cache Hit: {cache_hit_rate}%\n"
        f"Report: {report_filename}\n"
    )
    _publish("Deep Insight Job Completed", message, job_id)


def _publish_failure_notification(job_id: str, job_record: dict, error_message: str):
    """Publish job failure notification to SNS topic."""
    user_query = job_record.get("user_query", "")[:100]
    message = (
        "Deep Insight Job Failed\n"
        "\n"
        f"Job ID: {job_id}\n"
        f"Status: Failed\n"
        f"Query: {user_query}\n"
        f"Error: {error_message[:500]}\n"
    )
    _publish("Deep Insight Job Failed", message, job_id)
