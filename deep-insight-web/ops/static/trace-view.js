// ==================== Trace view ====================
// Shared by the job list (side panel) and the job page. Builds a tree of
// observations from the runtime's events.jsonl records and renders it as
// tree (left) + detail (right), like Langfuse's trace view:
//
//   Trace                      the job: request in, final answer out
//     Agent                    one agent invocation (agent_start .. agent_end)
//       Tool                   one tool call (tool_use .. tool_result)
//         Agent                a sub-agent the tool ran (Supervisor -> Coder)
//     Plan review              a HITL round: plan shown, user's answer
//
// Traces recorded before agent_start/agent_end existed still render: an
// agent's records open an implicit invocation, closed by its usage record.
// Needs admin-i18n.js (t).

var TraceView = (function() {
    var AGENT_COLORS = {
        coordinator: '#a78bfa', planner: '#4a7cff', plan_reviewer: '#fbbf24', supervisor: '#f472b6',
        coder: '#34d399', validator: '#22d3ee', reporter: '#fb923c', tracker: '#94a3b8', auditor: '#f87171'
    };
    // Keys whose string values are code or file content
    var CODE_KEYS = ['code', 'content', 'cmd', 'command', 'script'];

    // ---------- Helpers ----------

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function agentLabel(agent) {
        return (agent || 'system').split('_').map(function(w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join(' ');
    }

    function color(agent) {
        return AGENT_COLORS[agent] || 'var(--accent)';
    }

    function seconds(fromIso, toIso) {
        var d = (Date.parse(toIso) - Date.parse(fromIso)) / 1000;
        return isNaN(d) ? 0 : d;
    }

    function formatSeconds(s) {
        if (s == null) return '-';
        if (s < 1) return '<1s';
        if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + 's';
        return Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's';
    }

    function formatCompact(n) {
        if (!n) return '-';
        if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
        if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'k';
        return String(n);
    }

    function formatClock(iso) {
        var d = new Date(iso);
        if (isNaN(d)) return '-';
        return d.toLocaleTimeString([], { hour12: false });
    }

    function preview(value, length) {
        var text = typeof value === 'string' ? value : JSON.stringify(value);
        text = (text || '').replace(/\s+/g, ' ').trim();
        length = length || 120;
        return text.length > length ? text.slice(0, length) + '…' : text;
    }

    function parseJson(text) {
        if (typeof text !== 'string') return null;
        var trimmed = text.trim();
        if (trimmed.charAt(0) !== '{' && trimmed.charAt(0) !== '[') return null;
        try { return JSON.parse(trimmed); } catch (e) { return null; }
    }

    function usageTotal(u) {
        if (!u) return 0;
        return (u.input_tokens || 0) + (u.output_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_write_input_tokens || 0);
    }

    // Collapsible JSON tree. Multi-line strings and code keys render as blocks.
    function jsonTree(value, key, depth) {
        depth = depth || 0;
        if (!(value && typeof value === 'object')) {
            var row = el('div', 'json-row');
            if (key != null) row.appendChild(el('span', 'json-key', key + ': '));
            if (typeof value === 'string' && (value.indexOf('\n') >= 0 || CODE_KEYS.indexOf(key) >= 0)) {
                row.appendChild(el('div', 'tv-pre code', value));
            } else {
                var cls = 'json-' + (value === null ? 'null' : typeof value);
                row.appendChild(el('span', cls, typeof value === 'string' ? '"' + value + '"' : String(value)));
            }
            return row;
        }
        var isArray = Array.isArray(value);
        var keys = Object.keys(value);
        var node = el('details', 'json-node');
        node.open = depth < 2;
        var summary = el('summary');
        if (key != null) summary.appendChild(el('span', 'json-key', key + ': '));
        summary.appendChild(el('span', 'json-meta', isArray ? '[' + keys.length + ']' : '{' + keys.length + '}'));
        node.appendChild(summary);
        var children = el('div', 'json-children');
        keys.forEach(function(k) { children.appendChild(jsonTree(value[k], isArray ? Number(k) : k, depth + 1)); });
        node.appendChild(children);
        return node;
    }

    function decisionBadge(decision) {
        var tone = decision === 'approved' ? 'ok' : decision === 'revision_requested' ? 'warn' : 'muted';
        return el('span', 'tv-badge tv-badge-' + tone, t('hitl_' + decision));
    }

    // ---------- Tree ----------

    function buildTree(records) {
        var root = { id: 'root', type: 'trace', name: 'Trace', children: [], items: [], start: null, end: null };
        var stack = [root];        // open observations, innermost last
        var openTools = {};
        var seq = 0;

        function newNode(props, parent) {
            var node = Object.assign({ id: 'n' + (++seq), children: [], items: [], end: null }, props);
            node.parent = parent;
            parent.children.push(node);
            return node;
        }
        function top() { return stack[stack.length - 1]; }
        function close(node, ts) {
            if (!node.end) node.end = ts;
            var i = stack.indexOf(node);
            // anything opened inside it and never closed ends with it
            if (i >= 0) stack.splice(i).forEach(function(n) { if (!n.end) n.end = ts; });
        }
        // Innermost open invocation of this agent; opens an implicit one if none
        function invocation(agent, ts) {
            for (var i = stack.length - 1; i >= 0; i--) {
                if (stack[i].type === 'agent' && stack[i].agent === agent) return stack[i];
            }
            var node = newNode({ type: 'agent', agent: agent, name: agentLabel(agent), start: ts, implicit: true }, top());
            stack.push(node);
            return node;
        }

        records.forEach(function(r) {
            var ts = r.ts;
            if (ts) {
                if (!root.start || ts < root.start) root.start = ts;
                var last = r.end_ts || ts;
                if (!root.end || last > root.end) root.end = last;
            }
            var agent = r.agent || 'system';
            if (r.kind === 'input') {
                root.input = r.prompt;
                root.meta = { job_id: r.job_id, request_id: r.request_id, data_directory: r.data_directory };
            } else if (r.kind === 'agent_start') {
                stack.push(newNode({ type: 'agent', agent: agent, name: agentLabel(agent), start: ts, input: r.input }, top()));
            } else if (r.kind === 'agent_end') {
                var inv = invocation(agent, ts);
                inv.error = r.error;
                close(inv, ts);
            } else if (r.kind === 'tool_use') {
                var owner = invocation(agent, ts);
                var tool = newNode({ type: 'tool', agent: agent, name: r.tool || 'tool', start: ts, input: r.input, tool_id: r.tool_id }, owner);
                owner.items.push({ tool: tool });
                stack.push(tool);
                if (r.tool_id) openTools[r.tool_id] = tool;
            } else if (r.kind === 'tool_result') {
                var call = openTools[r.tool_id];
                if (!call) {
                    var host = invocation(agent, ts);
                    call = newNode({ type: 'tool', agent: agent, name: r.tool || 'tool', start: ts, tool_id: r.tool_id }, host);
                    host.items.push({ tool: call });
                }
                call.output = r.output;
                close(call, ts);
            } else if (r.kind === 'text' || r.kind === 'reasoning') {
                var speaker = invocation(agent, ts);
                speaker.items.push({ record: r });
                if (r.kind === 'text' && r.text && r.text.trim()) {
                    speaker.output = r.text;
                    root.output = r.text;  // the run's final answer: its last text, as in output_preview
                }
            } else if (r.kind === 'usage') {
                var spent = invocation(agent, ts);
                spent.usage = r;
                if (spent.implicit) close(spent, ts);
            } else if (r.kind === 'plan_review') {
                stack.push(newNode({ type: 'hitl', agent: 'plan_reviewer', name: t('trace_plan_feedback') + ' #' + ((r.revision_count || 0) + 1),
                    start: ts, plan: r.plan, revision_count: r.revision_count }, top()));
            } else if (r.kind === 'plan_feedback') {
                var review = null;
                for (var i = stack.length - 1; i >= 0; i--) { if (stack[i].type === 'hitl') { review = stack[i]; break; } }
                if (!review) review = newNode({ type: 'hitl', agent: 'plan_reviewer', name: t('trace_plan_feedback'), start: ts }, top());
                review.decision = r.decision;
                review.feedback = r.feedback;
                review.waited_seconds = r.waited_seconds;
                close(review, ts);
            }
        });

        // Tokens per subtree
        (function sum(node) {
            node.tokens = usageTotal(node.usage) + node.children.reduce(function(n, c) { return n + sum(c); }, 0);
            return node.tokens;
        })(root);
        return root;
    }

    function walk(node, fn, depth) {
        depth = depth || 0;
        fn(node, depth);
        node.children.forEach(function(c) { walk(c, fn, depth + 1); });
    }

    function findNode(root, id) {
        var found = null;
        walk(root, function(n) { if (n.id === id) found = n; });
        return found;
    }

    // ---------- View tree: agents only ----------
    // Tool calls move into the detail pane. An agent that delegates to
    // sub-agents (the Supervisor) is split into turns around each delegation,
    // so its interventions between agents show in the tree:
    //   Supervisor > [turn #1 → Coder] Coder [turn #2 → Tracker] Tracker ...

    function subAgents(tool) {
        return tool.children.filter(function(c) { return c.type === 'agent'; });
    }

    function itemStart(it) {
        if (it.record) return it.record.ts;
        if (it.returned) return it.returned.end;
        return (it.tool || it.delegate).start;
    }

    function itemEnd(it) {
        if (it.record) return it.record.end_ts || it.record.ts;
        if (it.delegate) return it.delegate.start;
        if (it.returned) return it.returned.end;
        return it.tool.end;
    }

    function viewTree(root) {
        function view(node) {
            var v = { id: node.id, kind: node.type, ref: node, children: [], start: node.start, end: node.end, tokens: node.tokens };
            if (node.type === 'trace') {
                node.children.forEach(function(c) {
                    if (c.type === 'tool') subAgents(c).forEach(function(a) { v.children.push(view(a)); });
                    else v.children.push(view(c));
                });
                return v;
            }
            if (node.type !== 'agent') return v;

            var delegates = node.items.some(function(it) { return it.tool && subAgents(it.tool).length; });
            if (delegates) {
                var segment = [], n = 0;
                var flush = function(next) {
                    if (!segment.length) return;
                    n++;
                    v.children.push({
                        id: node.id + '-t' + n, kind: 'turn', ref: node, items: segment, n: n, next: next,
                        start: itemStart(segment[0]), end: itemEnd(segment[segment.length - 1]) || null, tokens: 0, children: []
                    });
                    segment = [];
                };
                node.items.forEach(function(it) {
                    var subs = it.tool ? subAgents(it.tool) : [];
                    if (!subs.length) { segment.push(it); return; }
                    segment.push({ delegate: it.tool });
                    flush(subs);
                    subs.forEach(function(a) { v.children.push(view(a)); });
                    if (it.tool.end) segment.push({ returned: it.tool });
                });
                flush(null);
            }
            // agents started directly by this one, not through a tool call
            node.children.forEach(function(c) { if (c.type === 'agent') v.children.push(view(c)); });
            return v;
        }
        return view(root);
    }

    // ---------- Detail ----------

    function section(title, open, body) {
        var d = el('details', 'tv-section');
        d.open = open;
        var summary = el('summary', null, title);
        d.appendChild(summary);
        var content = el('div', 'tv-section-body');
        if (body) content.appendChild(body);
        d.appendChild(content);
        return d;
    }

    function textBlock(text, className) {
        return el('div', className || 'tv-text', text == null || text === '' ? '-' : String(text));
    }


    // Code interpreter tools return "status||code||stdout"
    function splitResult(output) {
        var parts = typeof output === 'string' ? output.split('||') : [];
        return parts.length === 3 ? { status: parts[0], stdout: parts[2] } : null;
    }





    // ---------- Process as JSON ----------
    // An agent works in rounds: a response (its reasoning and message), then
    // the tool calls that response made; their results lead to the next round.
    // The rounds render as a JSON array in a devtools-style viewer: collapsed
    // nodes show a one-line preview.

    function roundsOf(items) {
        var rounds = [], cur = null;
        items.forEach(function(it) {
            if (it.record) {
                if (!cur || cur.calls.length) { cur = { records: [], calls: [] }; rounds.push(cur); }
                cur.records.push(it.record);
            } else {
                if (!cur) { cur = { records: [], calls: [] }; rounds.push(cur); }
                cur.calls.push(it);
            }
        });
        return rounds;
    }

    function callFailed(it) {
        var r = it.tool && splitResult(it.tool.output);
        return !!(r && r.status !== 'completed');
    }

    function agentNames(agents) {
        return agents.map(function(a) { return agentLabel(a.agent); }).join(', ');
    }

    function outputValue(output) {
        var r = splitResult(output);
        if (r) return r.stdout;
        var parsed = parseJson(output);
        return parsed != null ? parsed : (output == null ? null : String(output));
    }

    function duration(call) {
        return call.end ? formatSeconds(seconds(call.start, call.end)) : t('trace_running');
    }

    function callJson(it) {
        if (it.returned) {
            var back = it.returned, from = subAgents(back);
            var o = { tool_result: back.name };
            if (from.length) o.from = agentNames(from);
            o.output = outputValue(back.output);
            return o;
        }
        var call = it.tool || it.delegate, subs = subAgents(call), result = splitResult(call.output);
        var c = { tool: call.name };
        if (subs.length) c.agent = agentNames(subs);
        if (result) c.status = result.status;
        c.duration = duration(call);
        c.input = call.input == null ? null : call.input;
        if (!it.delegate) c.output = call.end ? outputValue(call.output) : t('trace_running');
        return c;
    }

    function roundJson(round) {
        var o = {};
        var texts = round.records.filter(function(r) { return r.kind === 'text'; }).map(function(r) { return r.text; });
        var thoughts = round.records.filter(function(r) { return r.kind === 'reasoning'; }).map(function(r) { return r.text; });
        if (thoughts.length) o.reasoning = thoughts.join('\n');
        if (texts.length) o.response = texts.join('\n');
        if (round.calls.length) o.tool_calls = round.calls.map(callJson);
        return o;
    }

    function jvPreview(value, budget) {
        budget = budget || 90;
        if (value === null) return 'null';
        if (typeof value === 'string') return '"' + preview(value, 40) + '"';
        if (typeof value !== 'object') return String(value);
        if (Array.isArray(value)) return '[' + value.length + ']';
        var parts = [], used = 0;
        Object.keys(value).some(function(k) {
            var v = value[k];
            var p = k + ': ' + (v && typeof v === 'object' ? (Array.isArray(v) ? '[' + v.length + ']' : '{…}') : jvPreview(v));
            if (used + p.length > budget && parts.length) { parts.push('…'); return true; }
            parts.push(p);
            used += p.length;
            return false;
        });
        return '{' + parts.join(', ') + '}';
    }

    function jvKey(row, key) {
        if (key == null) return;
        row.appendChild(el('span', typeof key === 'number' ? 'jv-index' : 'jv-key', key + ': '));
    }

    function jvValue(value, key, path, ctx, depth) {
        if (!(value && typeof value === 'object')) {
            var row = el('div', 'jv-row');
            jvKey(row, key);
            if (typeof value === 'string' && (value.indexOf('\n') >= 0 || value.length > 160 || CODE_KEYS.indexOf(key) >= 0)) {
                row.appendChild(el('div', 'jv-block' + (CODE_KEYS.indexOf(key) >= 0 ? ' code' : ''), value));
                return row;
            }
            var cls = value === null ? 'jv-null' : typeof value === 'string' ? 'jv-str' : typeof value === 'number' ? 'jv-num' : 'jv-bool';
            if (key === 'status') cls += value === 'completed' ? ' jv-ok' : ' jv-fail';
            row.appendChild(el('span', cls, typeof value === 'string' ? '"' + value + '"' : String(value)));
            return row;
        }
        var isArray = Array.isArray(value);
        var node = el('details', 'jv-node');
        var stateKey = 'jv:' + path;
        node.open = stateKey in ctx.state.open ? ctx.state.open[stateKey] : depth === 0;
        node.addEventListener('toggle', function() { ctx.state.open[stateKey] = node.open; });
        var summary = el('summary', 'jv-row');
        jvKey(summary, key);
        summary.appendChild(el('span', 'jv-preview', jvPreview(value)));
        summary.appendChild(el('span', 'jv-open', isArray ? '[' : '{'));
        node.appendChild(summary);
        var children = el('div', 'jv-children');
        (isArray ? value.map(function(_, i) { return i; }) : Object.keys(value)).forEach(function(k) {
            children.appendChild(jvValue(value[k], k, path + '.' + k, ctx, depth + 1));
        });
        node.appendChild(children);
        node.appendChild(el('div', 'jv-close', isArray ? ']' : '}'));
        return node;
    }

    function processSection(items, ctx, key) {
        var rounds = roundsOf(items);
        var calls = items.filter(function(it) { return it.tool || it.delegate; });
        var failed = items.filter(callFailed).length;

        var wrap = el('div');
        var bar = el('div', 'tv-process-actions');
        var summary = el('span', 'tv-process-summary',
            t('tv_rounds') + ' ' + rounds.length + ' · ' + t('trace_tool_call') + ' ' + calls.length + (failed ? ' · ' : ''));
        if (failed) summary.appendChild(el('span', 'tv-badge tv-badge-fail', t('tv_failed') + ' ' + failed));
        bar.appendChild(summary);
        var expand = el('button', 'tv-link', t('trace_expand_all'));
        var collapse = el('button', 'tv-link', t('trace_collapse_all'));
        bar.appendChild(expand);
        bar.appendChild(collapse);
        wrap.appendChild(bar);

        var viewer = el('div', 'jv');
        viewer.appendChild(jvValue(rounds.map(roundJson), null, key, ctx, 0));
        expand.onclick = function() {
            viewer.querySelectorAll('details.jv-node').forEach(function(d) { d.open = true; });
        };
        collapse.onclick = function() {
            viewer.querySelectorAll('details.jv-node').forEach(function(d, i) { d.open = i === 0; });
        };
        wrap.appendChild(viewer);
        return section(t('tv_process'), true, wrap);
    }

    function metadata(v, job) {
        var node = v.ref, meta = {};
        if (v.kind === 'trace') {
            Object.assign(meta, node.meta || {});
            if (job) {
                meta.status = job.status;
                meta.session_id = job.session_id;
                meta.total_tokens = job.total_tokens;
                meta.cache_hit_rate = job.cache_hit_rate;
            }
        } else {
            meta.type = v.kind === 'turn' ? 'turn' : node.type;
            if (node.agent) meta.agent = node.agent;
        }
        meta.start = v.start;
        meta.end = v.end;
        meta.latency_seconds = v.start && v.end ? Math.round(seconds(v.start, v.end) * 10) / 10 : null;
        if (v.kind !== 'turn') {
            if (node.usage) {
                meta.model_id = node.usage.model_id;
                meta.tokens = {
                    input: node.usage.input_tokens, output: node.usage.output_tokens,
                    cache_read: node.usage.cache_read_input_tokens, cache_write: node.usage.cache_write_input_tokens
                };
            }
            if (node.tokens) meta.tokens_including_sub_agents = node.tokens;
            if (node.error) meta.error = node.error;
            if (node.waited_seconds != null) meta.waited_seconds = node.waited_seconds;
        }
        return meta;
    }

    function viewName(v) {
        if (v.kind === 'trace') return t('tv_trace');
        if (v.kind === 'turn') {
            return agentLabel(v.ref.agent) + (v.next
                ? ' → ' + v.next.map(function(a) { return agentLabel(a.agent); }).join(', ')
                : ' · ' + t('tv_turn_final'));
        }
        return v.ref.name;
    }

    function stat(name, value) {
        var s = el('span', 'tv-stat');
        s.appendChild(el('span', 'tv-stat-label', name));
        s.appendChild(el('span', 'tv-stat-value', value));
        return s;
    }

    function viewIcon(v) {
        var icon = el('span', 'tv-icon tv-icon-' + v.kind);
        if (v.kind === 'agent' || v.kind === 'hitl') icon.style.background = color(v.ref.agent);
        if (v.kind === 'turn') icon.style.borderColor = color(v.ref.agent);
        if (v.kind === 'trace') icon.textContent = '◆';
        return icon;
    }

    function renderDetail(pane, v, ctx) {
        pane.replaceChildren();
        var node = v.ref;
        var header = el('div', 'tv-detail-header');
        var title = el('div', 'tv-detail-title');
        title.appendChild(viewIcon(v));
        title.appendChild(el('span', null, viewName(v)));
        header.appendChild(title);
        var stats = el('div', 'tv-stats');
        stats.appendChild(stat(t('tv_latency'), v.start && v.end ? formatSeconds(seconds(v.start, v.end)) : (v.kind === 'trace' ? '-' : t('trace_running'))));
        if (v.kind !== 'turn') stats.appendChild(stat(t('tv_tokens'), formatCompact(v.tokens)));
        if (v.kind === 'agent' && node.usage && node.usage.model_id) {
            stats.appendChild(stat(t('tv_model'), node.usage.model_id.replace(/^global\.anthropic\./, '')));
        }
        if (v.start) stats.appendChild(stat(t('tv_started'), formatClock(v.start)));
        if (node.decision) stats.appendChild(decisionBadge(node.decision));
        if (node.error) stats.appendChild(el('span', 'tv-badge tv-badge-fail', t('tv_error')));
        header.appendChild(stats);
        pane.appendChild(header);

        if (v.kind === 'trace') {
            pane.appendChild(section(t('tv_input'), true, textBlock(node.input || (ctx.job && ctx.job.user_query))));
            pane.appendChild(section(t('tv_output'), true, textBlock(node.output)));
            var reviews = [];
            walk(node, function(n) { if (n.type === 'hitl') reviews.push(n); });
            if (reviews.length) {
                var list = el('div', 'tv-process');
                reviews.forEach(function(r) {
                    var row = el('button', 'tv-step tv-step-button');
                    row.appendChild(el('span', 'tv-step-kind', r.name));
                    if (r.decision) row.appendChild(decisionBadge(r.decision));
                    row.appendChild(el('span', 'tv-step-preview', r.feedback ? '“' + preview(r.feedback, 100) + '”' : t('hitl_no_feedback')));
                    row.onclick = function() { ctx.select(r.id); };
                    list.appendChild(row);
                });
                pane.appendChild(section(t('hitl_title'), true, list));
            }
        } else if (v.kind === 'agent') {
            pane.appendChild(section(t('tv_input'), !node.implicit, node.implicit
                ? el('div', 'tv-empty', t('tv_input_not_recorded')) : textBlock(node.input)));
            pane.appendChild(processSection(node.items, ctx, v.id));
            pane.appendChild(section(t('tv_output'), true, textBlock(node.output)));
        } else if (v.kind === 'turn') {
            pane.appendChild(processSection(v.items, ctx, v.id));
        } else if (v.kind === 'hitl') {
            pane.appendChild(section(t('trace_plan'), true, textBlock(node.plan)));
            var answer = el('div', 'tv-hitl');
            if (node.decision) answer.appendChild(decisionBadge(node.decision));
            answer.appendChild(textBlock(node.feedback || (node.decision ? t('hitl_no_feedback') : t('trace_running')),
                node.feedback ? 'tv-text tv-feedback' : 'tv-empty'));
            pane.appendChild(section(t('hitl_feedback'), true, answer));
        }
        pane.appendChild(section(t('tv_metadata'), false, jsonTree(metadata(v, ctx.job))));
    }

    // ---------- View ----------

    // state: { selected, collapsed: {id: true}, open: {step: true}, treeScroll },
    // kept by the caller across refreshes
    function render(container, records, opts) {
        opts = opts || {};
        var state = opts.state || {};
        state.collapsed = state.collapsed || {};
        state.open = state.open || {};
        var running = !!opts.running;
        var root = viewTree(buildTree(records));
        var traceStart = Date.parse(root.start);
        var traceEnd = running ? Math.max(Date.parse(root.end) || 0, Date.now()) : Date.parse(root.end);
        var total = Math.max((traceEnd - traceStart) / 1000, 1);

        var byId = {}, parentOf = {};
        walk(root, function(n) { byId[n.id] = n; n.children.forEach(function(c) { parentOf[c.id] = n; }); });
        if (!byId[state.selected]) state.selected = 'root';

        var view = el('div', 'tv');
        var treePane = el('div', 'tv-tree');
        var detailPane = el('div', 'tv-detail');
        var ctx = {
            job: opts.job,
            state: state,
            select: function(id) {
                state.selected = id;
                for (var p = parentOf[id]; p; p = parentOf[p.id]) delete state.collapsed[p.id];
                draw();
                detailPane.scrollTop = 0;
            }
        };

        var head = el('div', 'tv-tree-head');
        head.appendChild(el('span', null, t('tv_name')));
        head.appendChild(el('span', 'tv-col-timeline', t('tv_timeline')));
        head.appendChild(el('span', 'tv-col-num', t('tv_latency')));
        head.appendChild(el('span', 'tv-col-num', t('tv_tokens')));

        function draw() {
            var scroll = treePane.scrollTop;
            treePane.replaceChildren(head);
            walk(root, function(v, depth) {
                for (var p = parentOf[v.id]; p; p = parentOf[p.id]) if (state.collapsed[p.id]) return;
                var row = el('div', 'tv-row tv-row-' + v.kind + (v.id === state.selected ? ' selected' : ''));
                if (v.kind !== 'trace') row.style.setProperty('--agent-color', color(v.ref.agent));
                var name = el('span', 'tv-name');
                name.style.paddingLeft = (depth * 16) + 'px';
                var caret = el('button', 'tv-caret', v.children.length ? (state.collapsed[v.id] ? '▸' : '▾') : '');
                caret.onclick = function(e) {
                    e.stopPropagation();
                    if (!v.children.length) return;
                    if (state.collapsed[v.id]) delete state.collapsed[v.id]; else state.collapsed[v.id] = true;
                    draw();
                };
                name.appendChild(caret);
                name.appendChild(viewIcon(v));
                var text = el('span', 'tv-label', viewName(v));
                text.title = viewName(v);
                name.appendChild(text);
                if (v.ref.decision) name.appendChild(decisionBadge(v.ref.decision));
                if (v.ref.error && v.kind === 'agent') name.appendChild(el('span', 'tv-badge tv-badge-fail', t('tv_error')));
                if (running && v.kind !== 'trace' && !v.end) name.appendChild(el('span', 'tv-badge tv-badge-warn', t('trace_running')));
                row.appendChild(name);

                // Timeline bar: offset and width relative to the whole trace
                var bar = el('span', 'tv-col-timeline tv-bar-track');
                var start = Date.parse(v.start);
                var end = v.end ? Date.parse(v.end) : traceEnd;
                if (!isNaN(start)) {
                    var b = el('span', 'tv-bar' + (v.end ? '' : ' open') + (v.kind === 'turn' ? ' turn' : ''));
                    b.style.left = Math.max(0, (start - traceStart) / 1000 / total * 100) + '%';
                    b.style.width = Math.max(0.6, (end - start) / 1000 / total * 100) + '%';
                    b.style.background = v.kind === 'trace' ? 'var(--accent)' : color(v.ref.agent);
                    bar.appendChild(b);
                }
                row.appendChild(bar);
                row.appendChild(el('span', 'tv-col-num', v.start && v.end ? formatSeconds(seconds(v.start, v.end))
                    : (v.kind === 'trace' && !running ? '-' : '…')));
                row.appendChild(el('span', 'tv-col-num', v.kind === 'turn' ? '' : formatCompact(v.tokens)));
                row.onclick = function() { ctx.select(v.id); };
                treePane.appendChild(row);
            });
            treePane.scrollTop = scroll;
            renderDetail(detailPane, byId[state.selected], ctx);
        }

        view.appendChild(treePane);
        view.appendChild(detailPane);
        container.appendChild(view);
        draw();
        if (state.treeScroll) treePane.scrollTop = state.treeScroll;
        treePane.addEventListener('scroll', function() { state.treeScroll = treePane.scrollTop; });
    }

    return {
        render: render,
        buildTree: buildTree,
        viewTree: viewTree,
        el: el,
        preview: preview,
        formatSeconds: formatSeconds,
        formatCompact: formatCompact
    };
})();
