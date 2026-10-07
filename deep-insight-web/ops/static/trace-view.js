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

    // ---------- Detail ----------

    function section(title, open, body) {
        var d = el('details', 'tv-section');
        d.open = open;
        d.appendChild(el('summary', null, title));
        var content = el('div', 'tv-section-body');
        if (body) content.appendChild(body);
        d.appendChild(content);
        return d;
    }

    function textBlock(text, className) {
        return el('div', className || 'tv-text', text == null || text === '' ? '-' : String(text));
    }

    function valueBlock(value) {
        if (value && typeof value === 'object') return jsonTree(value);
        var parsed = parseJson(value);
        if (parsed) return jsonTree(parsed);
        return textBlock(value, 'tv-pre');
    }

    // Code interpreter tools return "status||code||stdout"
    function toolOutput(output) {
        var parts = typeof output === 'string' ? output.split('||') : [];
        if (parts.length !== 3) return valueBlock(output);
        var box = el('div');
        var status = el('div', 'tv-kv');
        status.appendChild(el('span', 'tv-k', t('trace_status')));
        status.appendChild(el('span', 'tv-badge ' + (parts[0] === 'completed' ? 'tv-badge-ok' : 'tv-badge-fail'), parts[0]));
        box.appendChild(status);
        box.appendChild(el('div', 'tv-pre', parts[2] || '-'));
        return box;
    }

    function processList(node, onSelect) {
        var list = el('div', 'tv-process');
        node.items.forEach(function(item) {
            if (item.tool) {
                var call = item.tool;
                var row = el('button', 'tv-step tv-step-tool');
                row.appendChild(el('span', 'tv-step-kind', t('trace_tool_call')));
                row.appendChild(el('span', 'tv-tool-name', call.name));
                if (call.children.length) {
                    call.children.forEach(function(c) {
                        var chip = el('span', 'tv-agent-chip', agentLabel(c.agent));
                        chip.style.setProperty('--agent-color', color(c.agent));
                        row.appendChild(chip);
                    });
                }
                row.appendChild(el('span', 'tv-step-preview', preview(call.input && typeof call.input === 'object'
                    ? (call.input.code || call.input.task || call.input) : call.input, 90)));
                row.appendChild(el('span', 'tv-step-time', call.end ? formatSeconds(seconds(call.start, call.end)) : t('trace_running')));
                row.onclick = function() { onSelect(call.id); };
                list.appendChild(row);
                return;
            }
            var r = item.record;
            var d = el('details', 'tv-step');
            d.open = false;
            var summary = el('summary');
            summary.appendChild(el('span', 'tv-step-kind', r.kind === 'text' ? t('trace_text') : t('trace_reasoning')));
            summary.appendChild(el('span', 'tv-step-preview', preview(r.text, 140)));
            d.appendChild(summary);
            d.appendChild(textBlock(r.text, r.kind === 'reasoning' ? 'tv-text muted' : 'tv-text'));
            list.appendChild(d);
        });
        if (!node.items.length) list.appendChild(el('div', 'tv-empty', t('files_none')));
        return list;
    }

    function metadata(node, job) {
        var meta = {};
        if (node.type === 'trace') {
            Object.assign(meta, node.meta || {});
            if (job) {
                meta.status = job.status;
                meta.session_id = job.session_id;
                meta.total_tokens = job.total_tokens;
                meta.cache_hit_rate = job.cache_hit_rate;
            }
        } else {
            meta.type = node.type;
            if (node.agent) meta.agent = node.agent;
            if (node.tool_id) meta.tool_id = node.tool_id;
        }
        meta.start = node.start;
        meta.end = node.end;
        meta.latency_seconds = node.start && node.end ? Math.round(seconds(node.start, node.end) * 10) / 10 : null;
        if (node.usage) {
            meta.model_id = node.usage.model_id;
            meta.tokens = {
                input: node.usage.input_tokens, output: node.usage.output_tokens,
                cache_read: node.usage.cache_read_input_tokens, cache_write: node.usage.cache_write_input_tokens
            };
        } else if (node.tokens) {
            meta.tokens_in_subtree = node.tokens;
        }
        if (node.error) meta.error = node.error;
        if (node.waited_seconds != null) meta.waited_seconds = node.waited_seconds;
        return meta;
    }

    function renderDetail(pane, node, ctx) {
        pane.replaceChildren();
        var header = el('div', 'tv-detail-header');
        var title = el('div', 'tv-detail-title');
        title.appendChild(nodeIcon(node));
        title.appendChild(el('span', null, node.type === 'trace' ? t('tv_trace') : node.name));
        header.appendChild(title);
        var stats = el('div', 'tv-stats');
        var latency = node.start && node.end ? formatSeconds(seconds(node.start, node.end)) : (node.type === 'trace' ? '-' : t('trace_running'));
        stats.appendChild(stat(t('tv_latency'), latency));
        stats.appendChild(stat(t('tv_tokens'), formatCompact(node.tokens)));
        if (node.usage && node.usage.model_id) stats.appendChild(stat(t('tv_model'), node.usage.model_id.replace(/^global\.anthropic\./, '')));
        if (node.start) stats.appendChild(stat(t('tv_started'), formatClock(node.start)));
        if (node.decision) stats.appendChild(decisionBadge(node.decision));
        if (node.error) stats.appendChild(el('span', 'tv-badge tv-badge-fail', t('tv_error')));
        header.appendChild(stats);
        pane.appendChild(header);

        if (node.type === 'trace') {
            pane.appendChild(section(t('tv_input'), true, textBlock(node.input || (ctx.job && ctx.job.user_query))));
            pane.appendChild(section(t('tv_output'), true, textBlock(node.output)));
            var reviews = [];
            walk(node, function(n) { if (n.type === 'hitl') reviews.push(n); });
            if (reviews.length) {
                var list = el('div');
                reviews.forEach(function(r) {
                    var row = el('button', 'tv-step tv-step-tool');
                    row.appendChild(el('span', 'tv-step-kind', r.name));
                    if (r.decision) row.appendChild(decisionBadge(r.decision));
                    row.appendChild(el('span', 'tv-step-preview', r.feedback ? '“' + preview(r.feedback, 100) + '”' : t('hitl_no_feedback')));
                    row.onclick = function() { ctx.select(r.id); };
                    list.appendChild(row);
                });
                pane.appendChild(section(t('hitl_title'), true, list));
            }
        } else if (node.type === 'agent') {
            pane.appendChild(section(t('tv_input'), !node.implicit, node.implicit
                ? el('div', 'tv-empty', t('tv_input_not_recorded')) : textBlock(node.input)));
            pane.appendChild(section(t('tv_process') + ' (' + node.items.length + ')', true, processList(node, ctx.select)));
            pane.appendChild(section(t('tv_output'), true, textBlock(node.output)));
        } else if (node.type === 'tool') {
            pane.appendChild(section(t('tv_input'), true, node.input == null ? textBlock('-') : valueBlock(node.input)));
            if (node.children.length) {
                var agents = el('div');
                node.children.forEach(function(c) {
                    var row = el('button', 'tv-step tv-step-tool');
                    var chip = el('span', 'tv-agent-chip', agentLabel(c.agent));
                    chip.style.setProperty('--agent-color', color(c.agent));
                    row.appendChild(chip);
                    row.appendChild(el('span', 'tv-step-preview', preview(c.output, 120)));
                    row.appendChild(el('span', 'tv-step-time', c.end ? formatSeconds(seconds(c.start, c.end)) : t('trace_running')));
                    row.onclick = function() { ctx.select(c.id); };
                    agents.appendChild(row);
                });
                pane.appendChild(section(t('tv_sub_agents'), true, agents));
            }
            pane.appendChild(section(t('tv_output'), true, node.end ? toolOutput(node.output) : el('div', 'tv-empty', t('trace_running'))));
        } else if (node.type === 'hitl') {
            pane.appendChild(section(t('trace_plan'), true, textBlock(node.plan)));
            var answer = el('div', 'tv-hitl');
            if (node.decision) answer.appendChild(decisionBadge(node.decision));
            answer.appendChild(textBlock(node.feedback || (node.decision ? t('hitl_no_feedback') : t('trace_running')),
                node.feedback ? 'tv-text tv-feedback' : 'tv-empty'));
            pane.appendChild(section(t('hitl_feedback'), true, answer));
        }
        pane.appendChild(section(t('tv_metadata'), false, jsonTree(metadata(node, ctx.job))));
    }

    function stat(label, value) {
        var s = el('span', 'tv-stat');
        s.appendChild(el('span', 'tv-stat-label', label));
        s.appendChild(el('span', 'tv-stat-value', value));
        return s;
    }

    function nodeIcon(node) {
        var icon = el('span', 'tv-icon tv-icon-' + node.type);
        if (node.type === 'agent' || node.type === 'hitl') icon.style.background = color(node.agent);
        if (node.type === 'tool') icon.textContent = '⚙';
        if (node.type === 'trace') icon.textContent = '◆';
        return icon;
    }

    // ---------- View ----------

    // state: { selected, collapsed: {id: true}, treeScroll } kept by the caller across refreshes
    function render(container, records, opts) {
        opts = opts || {};
        var state = opts.state || {};
        state.collapsed = state.collapsed || {};
        var root = buildTree(records);
        var running = !!opts.running;
        var traceStart = Date.parse(root.start);
        var traceEnd = running ? Math.max(Date.parse(root.end) || 0, Date.now()) : Date.parse(root.end);
        var total = Math.max((traceEnd - traceStart) / 1000, 1);
        if (!findNode(root, state.selected)) state.selected = 'root';

        var view = el('div', 'tv');
        var treePane = el('div', 'tv-tree');
        var detailPane = el('div', 'tv-detail');

        var ctx = {
            job: opts.job,
            select: function(id) {
                state.selected = id;
                // reveal: expand the ancestors of the selected node
                var n = findNode(root, id);
                for (var p = n && n.parent; p; p = p.parent) delete state.collapsed[p.id];
                draw();
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
            walk(root, function(node, depth) {
                for (var p = node.parent; p; p = p.parent) if (state.collapsed[p.id]) return;
                var row = el('div', 'tv-row' + (node.id === state.selected ? ' selected' : ''));
                if (node.type === 'agent' || node.type === 'hitl') row.style.setProperty('--agent-color', color(node.agent));
                var name = el('span', 'tv-name');
                name.style.paddingLeft = (depth * 16) + 'px';
                var caret = el('button', 'tv-caret', node.children.length ? (state.collapsed[node.id] ? '▸' : '▾') : '');
                caret.onclick = function(e) {
                    e.stopPropagation();
                    if (!node.children.length) return;
                    if (state.collapsed[node.id]) delete state.collapsed[node.id]; else state.collapsed[node.id] = true;
                    draw();
                };
                name.appendChild(caret);
                name.appendChild(nodeIcon(node));
                var label = el('span', 'tv-label', node.type === 'trace' ? t('tv_trace') : node.name);
                label.title = label.textContent;
                name.appendChild(label);
                if (node.decision) name.appendChild(decisionBadge(node.decision));
                if (node.error) name.appendChild(el('span', 'tv-badge tv-badge-fail', t('tv_error')));
                if (running && node.type !== 'trace' && !node.end) name.appendChild(el('span', 'tv-badge tv-badge-warn', t('trace_running')));
                row.appendChild(name);

                // Timeline bar: offset and width relative to the whole trace
                var bar = el('span', 'tv-col-timeline tv-bar-track');
                var start = Date.parse(node.start);
                var end = node.end ? Date.parse(node.end) : traceEnd;
                if (!isNaN(start)) {
                    var b = el('span', 'tv-bar' + (node.end ? '' : ' open'));
                    b.style.left = Math.max(0, (start - traceStart) / 1000 / total * 100) + '%';
                    b.style.width = Math.max(0.6, (end - start) / 1000 / total * 100) + '%';
                    b.style.background = node.type === 'tool' ? 'var(--amber)' : node.type === 'trace' ? 'var(--accent)' : color(node.agent);
                    bar.appendChild(b);
                }
                row.appendChild(bar);
                row.appendChild(el('span', 'tv-col-num', node.start && node.end ? formatSeconds(seconds(node.start, node.end))
                    : (node.type === 'trace' && !running ? '-' : '…')));
                row.appendChild(el('span', 'tv-col-num', formatCompact(node.tokens)));
                row.onclick = function() { ctx.select(node.id); };
                treePane.appendChild(row);
            });
            treePane.scrollTop = scroll;
            renderDetail(detailPane, findNode(root, state.selected), ctx);
        }

        view.appendChild(treePane);
        view.appendChild(detailPane);
        container.appendChild(view);
        draw();
        if (state.treeScroll) treePane.scrollTop = state.treeScroll;
        treePane.addEventListener('scroll', function() { state.treeScroll = treePane.scrollTop; });
        return root;
    }

    return {
        render: render,
        buildTree: buildTree,
        el: el,
        preview: preview,
        formatSeconds: formatSeconds,
        formatCompact: formatCompact
    };
})();
