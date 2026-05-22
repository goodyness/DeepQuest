"""
dashboard/app.py — DeepQuest Web Dashboard

A simple Flask web dashboard that shows system status and lets you
run seeders and the generator from a browser.

Usage:
    python dashboard/app.py
    Then open: http://localhost:5000

Requirements:
    pip install flask
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from flask import Flask, jsonify, render_template_string, request
except ImportError:
    print("Flask not installed. Run: pip install flask")
    sys.exit(1)

try:
    import asyncpg
    import asyncio
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

try:
    from neo4j import GraphDatabase
    HAS_NEO4J = True
except ImportError:
    HAS_NEO4J = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DeepQuest_Dashboard")

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUESTION_DIR = os.path.join(BASE_DIR, "question_generated")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")

# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DeepQuest Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0f1117; color: #e2e8f0; min-height: 100vh; }
        .header { background: #1a1d2e; border-bottom: 1px solid #2d3748;
                  padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
        .header h1 { font-size: 20px; font-weight: 700; color: #63b3ed; }
        .header .subtitle { font-size: 13px; color: #718096; }
        .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: #1a1d2e; border: 1px solid #2d3748; border-radius: 8px; padding: 20px; }
        .card h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em;
                   color: #718096; margin-bottom: 8px; }
        .card .value { font-size: 32px; font-weight: 700; color: #63b3ed; }
        .card .label { font-size: 13px; color: #718096; margin-top: 4px; }
        .card.green .value { color: #68d391; }
        .card.yellow .value { color: #f6e05e; }
        .card.red .value { color: #fc8181; }
        .section { background: #1a1d2e; border: 1px solid #2d3748; border-radius: 8px;
                   padding: 20px; margin-bottom: 16px; }
        .section h2 { font-size: 16px; font-weight: 600; margin-bottom: 16px;
                      color: #e2e8f0; border-bottom: 1px solid #2d3748; padding-bottom: 12px; }
        .btn { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px;
               border-radius: 6px; border: none; cursor: pointer; font-size: 14px;
               font-weight: 500; transition: all 0.15s; text-decoration: none; }
        .btn-primary { background: #3182ce; color: white; }
        .btn-primary:hover { background: #2b6cb0; }
        .btn-secondary { background: #2d3748; color: #e2e8f0; }
        .btn-secondary:hover { background: #4a5568; }
        .btn-success { background: #276749; color: white; }
        .btn-success:hover { background: #22543d; }
        .btn-warning { background: #744210; color: white; }
        .btn-warning:hover { background: #5f370e; }
        .btn-group { display: flex; flex-wrap: wrap; gap: 8px; }
        .question-list { display: flex; flex-direction: column; gap: 12px; }
        .question-item { background: #0f1117; border: 1px solid #2d3748; border-radius: 6px;
                         padding: 16px; }
        .question-item .answer { font-size: 11px; text-transform: uppercase;
                                  letter-spacing: 0.05em; color: #68d391; margin-bottom: 6px; }
        .question-item .prompt { font-size: 14px; color: #e2e8f0; line-height: 1.5; }
        .question-item .meta { font-size: 12px; color: #718096; margin-top: 8px; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
        .status-dot.green { background: #68d391; }
        .status-dot.red { background: #fc8181; }
        .status-dot.yellow { background: #f6e05e; }
        .log-output { background: #0f1117; border: 1px solid #2d3748; border-radius: 6px;
                      padding: 12px; font-family: monospace; font-size: 12px; color: #a0aec0;
                      max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
        .refresh-note { font-size: 12px; color: #718096; margin-top: 8px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; padding: 8px 12px; color: #718096; font-weight: 500;
             border-bottom: 1px solid #2d3748; }
        td { padding: 8px 12px; border-bottom: 1px solid #1a1d2e; }
        tr:hover td { background: #1a1d2e; }
        .tag { display: inline-block; padding: 2px 8px; border-radius: 4px;
               font-size: 11px; font-weight: 500; }
        .tag-blue { background: #2b4c7e; color: #90cdf4; }
        .tag-green { background: #1c4532; color: #9ae6b4; }
        .tag-yellow { background: #5f370e; color: #faf089; }
        #run-output { display: none; }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>⚡ DeepQuest</h1>
            <div class="subtitle">Adversarial Historical QA Generator</div>
        </div>
        <div style="margin-left: auto; font-size: 13px; color: #718096;">
            Last updated: <span id="last-updated">loading...</span>
        </div>
    </div>

    <div class="container">

        <!-- Stats Grid -->
        <div class="grid" id="stats-grid">
            <div class="card">
                <h3>Graph Nodes</h3>
                <div class="value" id="stat-nodes">—</div>
                <div class="label">Entity nodes in Neo4j</div>
            </div>
            <div class="card">
                <h3>Relationships</h3>
                <div class="value" id="stat-rels">—</div>
                <div class="label">Edges in knowledge graph</div>
            </div>
            <div class="card green">
                <h3>Max Source Domains</h3>
                <div class="value" id="stat-max-domains">—</div>
                <div class="label">Best edge (need 6+ for production)</div>
            </div>
            <div class="card">
                <h3>Pages Crawled</h3>
                <div class="value" id="stat-pages">—</div>
                <div class="label">In PostgreSQL</div>
            </div>
            <div class="card yellow">
                <h3>Pending Extraction</h3>
                <div class="value" id="stat-pending">—</div>
                <div class="label">Pages not yet processed</div>
            </div>
            <div class="card green">
                <h3>Questions Generated</h3>
                <div class="value" id="stat-questions">—</div>
                <div class="label">In question_generated/</div>
            </div>
        </div>

        <!-- Actions -->
        <div class="section">
            <h2>🚀 Run Seeders</h2>
            <div class="btn-group">
                <button class="btn btn-primary" onclick="runCommand('inject_infoboxes')">
                    📋 Inject Infoboxes
                </button>
                <button class="btn btn-primary" onclick="runCommand('inject_wikipedia')">
                    📖 Inject Wikipedia
                </button>
                <button class="btn btn-primary" onclick="runCommand('inject_corpus')">
                    📰 Inject Historical Corpus
                </button>
                <button class="btn btn-secondary" onclick="runCommand('inject_multisource')">
                    🌐 Inject Multi-Source
                </button>
                <button class="btn btn-warning" onclick="runCommand('merge_entities')">
                    🔗 Merge Entities
                </button>
            </div>
            <div id="run-output" style="margin-top: 12px;">
                <div class="log-output" id="run-log">Running...</div>
            </div>
        </div>

        <div class="section">
            <h2>🔍 Graph Analysis</h2>
            <div class="btn-group">
                <button class="btn btn-secondary" onclick="runCommand('detect_contradictions')">
                    ⚠️ Detect Contradictions
                </button>
                <button class="btn btn-secondary" onclick="loadContradictions()">
                    📋 Show Contradiction Report
                </button>
                <button class="btn btn-secondary" onclick="runCommand('merge_entities')">
                    🔗 Merge Entities
                </button>
            </div>
            <div id="contradiction-report" style="display:none; margin-top:12px;">
                <div class="log-output" id="contradiction-log">Loading...</div>
            </div>
        </div>
            <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px;">
                <div>
                    <label style="font-size: 12px; color: #718096;">Min Domains</label>
                    <input type="number" id="min-domains" value="6" min="1" max="8"
                           style="width: 60px; margin-left: 8px; background: #0f1117; border: 1px solid #2d3748;
                                  color: #e2e8f0; padding: 4px 8px; border-radius: 4px;">
                </div>
                <div>
                    <label style="font-size: 12px; color: #718096;">Min Sources</label>
                    <input type="number" id="min-sources" value="6" min="1" max="6"
                           style="width: 60px; margin-left: 8px; background: #0f1117; border: 1px solid #2d3748;
                                  color: #e2e8f0; padding: 4px 8px; border-radius: 4px;">
                </div>
                <div>
                    <label style="font-size: 12px; color: #718096;">
                        <input type="checkbox" id="skip-verify" checked style="margin-right: 4px;">
                        Skip Verification
                    </label>
                </div>
                <div>
                    <label style="font-size: 12px; color: #718096;">Min Year (optional)</label>
                    <input type="number" id="min-year" placeholder="e.g. 2000" min="1700" max="2024"
                           style="width: 100px; margin-left: 8px; background: #0f1117; border: 1px solid #2d3748;
                                  color: #e2e8f0; padding: 4px 8px; border-radius: 4px;">
                </div>
            </div>
            <div class="btn-group">
                <button class="btn btn-success" onclick="runGenerator()">
                    ✨ Generate Questions
                </button>
                <button class="btn btn-secondary" onclick="runCommand('export')">
                    📤 Export All Formats
                </button>
                <button class="btn btn-secondary" onclick="runCommand('benchmark')">
                    🎯 Run Benchmark
                </button>
            </div>
        </div>

        <!-- Recent Questions -->
        <div class="section">
            <h2>📝 Recent Questions</h2>
            <div class="question-list" id="question-list">
                <div style="color: #718096; font-size: 14px;">Loading...</div>
            </div>
        </div>

        <!-- Graph Status -->
        <div class="section">
            <h2>🔗 Top Graph Chains</h2>
            <table id="chains-table">
                <thead>
                    <tr>
                        <th>Entity A</th>
                        <th>Relation 1</th>
                        <th>Entity B (Answer)</th>
                        <th>Relation 2</th>
                        <th>Entity C</th>
                        <th>Domains</th>
                    </tr>
                </thead>
                <tbody id="chains-body">
                    <tr><td colspan="6" style="color: #718096;">Loading...</td></tr>
                </tbody>
            </table>
        </div>

    </div>

    <script>
        async function loadContradictions() {
            const report = document.getElementById('contradiction-report');
            const log = document.getElementById('contradiction-log');
            report.style.display = 'block';
            log.textContent = 'Loading contradiction report...';
            try {
                const r = await fetch('/api/contradictions');
                const data = await r.json();
                if (data.error) { log.textContent = data.error; return; }
                const c = data.contradictions;
                log.textContent = `Total: ${c.total} | High: ${c.high} | Medium: ${c.medium} | Low: ${c.low}\n\n`;
                if (c.items && c.items.length > 0) {
                    c.items.forEach(item => {
                        log.textContent += `[${item.severity?.toUpperCase()}] ${item.type}\n`;
                        if (item.chain) log.textContent += `  ${item.chain}\n`;
                        if (item.subject) log.textContent += `  ${item.subject} -${item.rel_type}-> ${item.object || item.org || '?'}\n`;
                        if (item.note) log.textContent += `  ${item.note}\n`;
                        log.textContent += '\n';
                    });
                } else {
                    log.textContent += 'No contradictions found.';
                }
            } catch(e) {
                log.textContent = 'Could not load report. Run detection first.';
            }
        }

        async function fetchStats() {
            try {
                const r = await fetch('/api/stats');
                const data = await r.json();
                document.getElementById('stat-nodes').textContent = data.nodes ?? '—';
                document.getElementById('stat-rels').textContent = data.relationships ?? '—';
                document.getElementById('stat-max-domains').textContent = data.max_domains ?? '—';
                document.getElementById('stat-pages').textContent = data.pages ?? '—';
                document.getElementById('stat-pending').textContent = data.pending ?? '—';
                document.getElementById('stat-questions').textContent = data.questions ?? '—';
                document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
            } catch(e) {
                console.error('Stats fetch failed:', e);
            }
        }

        async function fetchQuestions() {
            try {
                const r = await fetch('/api/questions');
                const data = await r.json();
                const list = document.getElementById('question-list');
                if (!data.questions || data.questions.length === 0) {
                    list.innerHTML = '<div style="color: #718096; font-size: 14px;">No questions generated yet. Run the generator to create some.</div>';
                    return;
                }
                list.innerHTML = data.questions.map(q => `
                    <div class="question-item">
                        <div class="answer">✓ ${q.answer}</div>
                        <div class="prompt">${q.prompt}</div>
                        <div class="meta">
                            <span class="tag tag-blue">${q.sources} sources</span>
                            ${q.year ? `<span class="tag tag-yellow" style="margin-left: 4px;">${q.year}</span>` : ''}
                            <span style="margin-left: 8px;">${q.filename}</span>
                        </div>
                    </div>
                `).join('');
            } catch(e) {
                console.error('Questions fetch failed:', e);
            }
        }

        async function fetchChains() {
            try {
                const r = await fetch('/api/chains');
                const data = await r.json();
                const tbody = document.getElementById('chains-body');
                if (!data.chains || data.chains.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" style="color: #718096;">No chains found. Run seeders to populate the graph.</td></tr>';
                    return;
                }
                tbody.innerHTML = data.chains.map(c => `
                    <tr>
                        <td>${c.a}</td>
                        <td><span class="tag tag-blue">${c.r1}</span></td>
                        <td style="color: #68d391; font-weight: 600;">${c.b}</td>
                        <td><span class="tag tag-blue">${c.r2}</span></td>
                        <td>${c.c}</td>
                        <td><span class="tag ${c.domains >= 6 ? 'tag-green' : c.domains >= 3 ? 'tag-yellow' : 'tag-blue'}">${c.domains}</span></td>
                    </tr>
                `).join('');
            } catch(e) {
                console.error('Chains fetch failed:', e);
            }
        }

        async function runCommand(cmd) {
            const output = document.getElementById('run-output');
            const log = document.getElementById('run-log');
            output.style.display = 'block';
            log.textContent = `Running ${cmd}...`;

            try {
                const r = await fetch('/api/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({command: cmd})
                });
                const data = await r.json();
                log.textContent = data.output || data.error || 'Done.';
                fetchStats();
                fetchQuestions();
                fetchChains();
            } catch(e) {
                log.textContent = 'Error: ' + e.message;
            }
        }

        async function runGenerator() {
            const minDomains = document.getElementById('min-domains').value;
            const minSources = document.getElementById('min-sources').value;
            const skipVerify = document.getElementById('skip-verify').checked;
            const minYear = document.getElementById('min-year').value;

            const output = document.getElementById('run-output');
            const log = document.getElementById('run-log');
            output.style.display = 'block';
            log.textContent = 'Running generator...';

            try {
                const r = await fetch('/api/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        command: 'generate',
                        min_domains: parseInt(minDomains),
                        min_sources: parseInt(minSources),
                        skip_verify: skipVerify,
                        min_year: minYear ? parseInt(minYear) : null,
                    })
                });
                const data = await r.json();
                log.textContent = data.output || data.error || 'Done.';
                fetchStats();
                fetchQuestions();
            } catch(e) {
                log.textContent = 'Error: ' + e.message;
            }
        }

        // Initial load
        fetchStats();
        fetchQuestions();
        fetchChains();

        // Auto-refresh every 30 seconds
        setInterval(() => {
            fetchStats();
            fetchQuestions();
            fetchChains();
        }, 30000);
    </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

def get_neo4j_stats():
    if not HAS_NEO4J:
        return {}
    try:
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", "deepquestpassword")
        )
        with driver.session() as session:
            nodes = session.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
            rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
            max_d = session.run(
                "MATCH ()-[r]->() RETURN max(size(coalesce(r.domains,[]))) AS m"
            ).single()["m"]
        driver.close()
        return {"nodes": nodes, "relationships": rels, "max_domains": max_d or 0}
    except Exception as e:
        return {"nodes": "err", "relationships": "err", "max_domains": "err"}


def get_postgres_stats():
    if not HAS_ASYNCPG:
        return {}
    try:
        async def _fetch():
            conn = await asyncpg.connect(
                "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"
            )
            total = await conn.fetchval("SELECT COUNT(*) FROM pages")
            pending = await conn.fetchval("SELECT COUNT(*) FROM pages WHERE processed = FALSE")
            await conn.close()
            return {"pages": total, "pending": pending}
        return asyncio.run(_fetch())
    except Exception:
        return {"pages": "err", "pending": "err"}


def get_question_count():
    q_dir = Path(QUESTION_DIR)
    if not q_dir.exists():
        return 0
    return len(list(q_dir.glob("question_*.txt")))


def get_recent_questions(limit=5):
    q_dir = Path(QUESTION_DIR)
    if not q_dir.exists():
        return []
    files = sorted(q_dir.glob("question_*.txt"), reverse=True)[:limit]
    questions = []
    for f in files:
        try:
            content = f.read_text(encoding='utf-8')
            import re
            prompt_match = re.search(r'PROMPT:\n(.*?)(?=\n\n[A-Z])', content, re.DOTALL)
            answer_match = re.search(r'ANSWER:\n(.+)', content)
            sources_match = re.findall(r'^\d+\.\s*(.+)$', content, re.MULTILINE)
            year_match = re.search(r'_(\d{4})\.txt$', str(f))
            questions.append({
                'filename': f.name,
                'prompt': (prompt_match.group(1).strip()[:200] + '...') if prompt_match else '',
                'answer': answer_match.group(1).strip() if answer_match else '',
                'sources': len(sources_match),
                'year': int(year_match.group(1)) if year_match else None,
            })
        except Exception:
            pass
    return questions


def get_top_chains(limit=10):
    if not HAS_NEO4J:
        return []
    try:
        driver = GraphDatabase.driver(
            "bolt://localhost:7687", auth=("neo4j", "deepquestpassword")
        )
        with driver.session() as session:
            result = session.run("""
                MATCH (a)-[r1]->(b)-[r2]->(c)
                WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name
                WITH a, r1, b, r2, c,
                     size(coalesce(r1.domains,[])) + size(coalesce(r2.domains,[])) AS domains
                ORDER BY domains DESC
                LIMIT $limit
                RETURN a.name AS a, type(r1) AS r1, b.name AS b,
                       type(r2) AS r2, c.name AS c, domains
            """, limit=limit)
            chains = [dict(r) for r in result]
        driver.close()
        return chains
    except Exception:
        return []


@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route('/api/stats')
def api_stats():
    stats = {}
    stats.update(get_neo4j_stats())
    stats.update(get_postgres_stats())
    stats['questions'] = get_question_count()
    return jsonify(stats)


@app.route('/api/questions')
def api_questions():
    return jsonify({'questions': get_recent_questions()})


@app.route('/api/chains')
def api_chains():
    return jsonify({'chains': get_top_chains()})


@app.route('/api/contradictions')
def api_contradictions():
    report_path = os.path.join(BASE_DIR, 'evaluator', 'contradictions.json')
    if not os.path.exists(report_path):
        return jsonify({'error': 'No contradiction report found. Run detection first.'})
    try:
        with open(report_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({'contradictions': {
            'total': data.get('total', 0),
            'high': data.get('by_severity', {}).get('high', 0),
            'medium': data.get('by_severity', {}).get('medium', 0),
            'low': data.get('by_severity', {}).get('low', 0),
            'items': data.get('contradictions', [])[:20],
        }})
    except Exception as e:
        return jsonify({'error': str(e)})


@app.route('/api/run', methods=['POST'])
def api_run():
    data = request.get_json()
    cmd = data.get('command', '')

    python = sys.executable
    base = BASE_DIR

    command_map = {
        'inject_infoboxes':   [python, 'seeder/inject_infoboxes.py'],
        'inject_wikipedia':   [python, 'seeder/inject_wikipedia.py',
                               '--topics-file', 'seeder/topics.txt', '--limit', '15'],
        'inject_corpus':      [python, 'seeder/inject_historical_corpus.py', '--limit', '20'],
        'inject_multisource': [python, 'seeder/inject_multisource.py',
                               '--topics-file', 'seeder/topics.txt', '--limit', '10'],
        'enrich_sources':     [python, 'seeder/enrich_sources.py', '--limit', '50'],
        'merge_entities':     [python, 'seeder/merge_entities.py'],
        'detect_contradictions': [python, 'seeder/detect_contradictions.py'],
        'export':             [python, 'evaluator/export.py'],
        'benchmark':          [python, 'evaluator/benchmark.py'],
    }

    if cmd == 'generate':
        args = [python, 'generator/query_engine.py',
                '--min-domains', str(data.get('min_domains', 6)),
                '--min-sources', str(data.get('min_sources', 6))]
        if data.get('skip_verify'):
            args.append('--skip-verify')
        if data.get('min_year'):
            args.extend(['--min-year', str(data['min_year'])])
        command_map['generate'] = args

    if cmd not in command_map:
        return jsonify({'error': f'Unknown command: {cmd}'}), 400

    try:
        result = subprocess.run(
            command_map[cmd],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        return jsonify({'output': output[-3000:] if len(output) > 3000 else output})
    except subprocess.TimeoutExpired:
        return jsonify({'output': 'Command timed out after 5 minutes.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("\n" + "="*50)
    print("  DeepQuest Dashboard")
    print("  Open: http://localhost:5000")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
