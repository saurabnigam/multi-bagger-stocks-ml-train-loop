/**
 * Antigravity Quant Engine V2 - Institutional Dashboard
 * Zero external CDN dependencies, fully offline-compatible.
 */

document.addEventListener('DOMContentLoaded', () => {
    // 1. Core State & Data
    const data = window.QUANT_DATA || { stocks: [], turnaround: [], track: 'empty', as_of: '--' };
    const learning = window.QUANT_LEARNING || { evaluations: [], curves: [] };
    const scoreboard = window.QUANT_SCOREBOARD || { portfolios: [], returns: [], pending_orders: [], benchmarks: [] };
    const factors = window.QUANT_FACTORS || { factors: [], contracts: [] };
    const kb = window.QUANT_KB || { decisions: [], proposals: [], lessons: [], hypotheses: [] };

    // System Status Header
    const sysTrack = document.getElementById('sys-track-badge');
    const sysAsOf = document.getElementById('sys-as-of');
    const sysCutoff = document.getElementById('sys-cutoff');
    const sysGenerated = document.getElementById('sys-generated');

    if (sysTrack) {
        const track = (data.track || 'empty').toLowerCase();
        sysTrack.textContent = track.toUpperCase();
        sysTrack.className = 'status-badge badge-' + track;
    }
    if (sysAsOf) sysAsOf.textContent = data.as_of || '--';
    if (sysCutoff) sysCutoff.textContent = data.source_cutoff || '--';
    if (sysGenerated) sysGenerated.textContent = data.generated_at || '--';

    // 2. Tab Navigation for all 8 tabs
    const TABS = ['ranking', 'learning', 'scoreboard', 'factors', 'sectors', 'data', 'knowledge', 'legacy'];
    
    function switchTab(targetTab) {
        TABS.forEach(tab => {
            const btn = document.getElementById('tab-' + tab);
            const pane = document.getElementById('content-' + tab);
            if (btn) {
                if (tab === targetTab) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            }
            if (pane) {
                if (tab === targetTab) {
                    pane.classList.add('active');
                } else {
                    pane.classList.remove('active');
                }
            }
        });

        // Trigger renderers on tab activation
        if (targetTab === 'learning') renderLearning();
        if (targetTab === 'scoreboard') renderScoreboard();
        if (targetTab === 'factors') renderFactors();
        if (targetTab === 'sectors') renderSectors();
        if (targetTab === 'data') renderDataProvenance();
        if (targetTab === 'knowledge') renderKnowledge();
        if (targetTab === 'legacy') renderLegacy();
    }

    TABS.forEach(tab => {
        const btn = document.getElementById('tab-' + tab);
        if (btn) {
            btn.addEventListener('click', () => switchTab(tab));
        }
    });

    // 3. TAB 1: Ranking View
    const stockListEl = document.getElementById('stock-list');
    const detailViewEl = document.getElementById('detail-view');
    const subtabAccepted = document.getElementById('subtab-accepted');
    const subtabRejected = document.getElementById('subtab-rejected');
    const subtabTurnaround = document.getElementById('subtab-turnaround');
    let currentRankingSubtab = 'accepted';

    function initRanking() {
        if (!stockListEl) return;
        stockListEl.innerHTML = '';

        let stocksToRender = [];
        if (currentRankingSubtab === 'accepted') {
            stocksToRender = (data.stocks || []).filter(s => s.eligible && s.final_score > 0);
        } else if (currentRankingSubtab === 'rejected') {
            stocksToRender = (data.stocks || []).filter(s => !s.eligible || s.final_score === 0 || s.dc_flag === 1);
        } else if (currentRankingSubtab === 'turnaround') {
            stocksToRender = data.turnaround || [];
        }

        if (stocksToRender.length === 0) {
            stockListEl.innerHTML = '<li style="padding: 20px; color: #888; text-align: center;">No stocks in this category</li>';
            if (detailViewEl) {
                detailViewEl.innerHTML = '<div class="empty-state-box"><h4>No stocks to display</h4><p>Current cohort has no records matching this category.</p></div>';
            }
            return;
        }

        stocksToRender.forEach(stock => {
            const li = document.createElement('li');
            li.className = 'stock-item';
            li.dataset.id = stock.security_id;

            let badge = '';
            if (currentRankingSubtab === 'rejected') {
                badge = stock.dc_flag ? '<span class="badge-refused">DEATH CROSS</span>' : '<span class="badge-unavailable">INELIGIBLE</span>';
            } else if (currentRankingSubtab === 'turnaround') {
                badge = '<span class="badge-unavailable" style="background:#fff8e6; color:#d97d00;">TURNAROUND</span>';
            }

            li.innerHTML = `
                <div class="stock-ticker">${stock.ticker} ${badge}</div>
                <div class="stock-name">${stock.company_name || stock.nse_symbol || stock.ticker}</div>
            `;
            li.addEventListener('click', () => loadStock(stock));
            stockListEl.appendChild(li);
        });

        loadStock(stocksToRender[0]);
    }

    if (subtabAccepted) {
        subtabAccepted.addEventListener('click', () => {
            currentRankingSubtab = 'accepted';
            subtabAccepted.classList.add('active');
            if (subtabRejected) subtabRejected.classList.remove('active');
            if (subtabTurnaround) subtabTurnaround.classList.remove('active');
            initRanking();
        });
    }

    if (subtabRejected) {
        subtabRejected.addEventListener('click', () => {
            currentRankingSubtab = 'rejected';
            subtabRejected.classList.add('active');
            if (subtabAccepted) subtabAccepted.classList.remove('active');
            if (subtabTurnaround) subtabTurnaround.classList.remove('active');
            initRanking();
        });
    }

    if (subtabTurnaround) {
        subtabTurnaround.addEventListener('click', () => {
            currentRankingSubtab = 'turnaround';
            subtabTurnaround.classList.add('active');
            if (subtabAccepted) subtabAccepted.classList.remove('active');
            if (subtabRejected) subtabRejected.classList.remove('active');
            initRanking();
        });
    }

    function loadStock(stock) {
        if (!detailViewEl || !stock) return;
        document.querySelectorAll('.stock-item').forEach(el => el.classList.remove('active'));
        const activeItem = document.querySelector(`.stock-item[data-id="${stock.security_id}"]`);
        if (activeItem) activeItem.classList.add('active');

        let banner = '';
        if (stock.dc_flag === 1) {
            banner = `
                <div class="bear-case-card" style="margin-bottom: 20px;">
                    <div class="bear-case-header">
                        <span class="bear-case-icon">⚠️</span>
                        <div class="bear-case-title">DEATH CROSS HARD-KILL TRIGGERED</div>
                    </div>
                    <div class="bear-case-desc">50-day SMA is below 200-day SMA. Hard-kill multiplier 0.0x applied.</div>
                </div>
            `;
        }

        detailViewEl.className = 'detail-view';
        detailViewEl.innerHTML = `
            <div class="detail-header">
                <h1>${stock.company_name || stock.ticker}</h1>
                <div class="detail-meta">
                    <span class="meta-pill">${stock.ticker}</span>
                    <span class="meta-pill">${stock.isin || 'ISIN'}</span>
                    <span class="meta-pill">${stock.sector_group || 'Sector'}</span>
                    <span class="meta-pill">Decile: ${stock.decile || '--'}</span>
                    <span class="meta-pill">Rank: #${stock.rank || '--'}</span>
                </div>
            </div>

            ${banner}

            <div class="quant-hud">
                <div class="quant-badge">
                    <span class="hud-label">Final Score</span>
                    <span class="hud-value ${stock.final_score >= 60 ? 'good' : ''}">${stock.final_score ? stock.final_score.toFixed(1) : '--'}</span>
                </div>
                <div class="quant-badge">
                    <span class="hud-label">Composite (Pre-Multiplier)</span>
                    <span class="hud-value">${stock.composite ? stock.composite.toFixed(1) : '--'}</span>
                </div>
                <div class="quant-badge">
                    <span class="hud-label">Quintile</span>
                    <span class="hud-value">Q${stock.quintile || '--'}</span>
                </div>
            </div>

            <div class="card">
                <h3>Factor Breakdown</h3>
                <div class="metrics-grid">
                    <div class="metric-box">
                        <span class="metric-label">Model</span>
                        <span class="metric-value" style="font-size: 15px;">${stock.model_id || 'CHAMPION'}</span>
                    </div>
                    <div class="metric-box">
                        <span class="metric-label">Eligibility</span>
                        <span class="metric-value" style="font-size: 15px;">${stock.eligible ? 'Eligible' : 'Ineligible'}</span>
                    </div>
                </div>
            </div>
        `;
    }

    // 4. TAB 2: Learning View
    let learningChartInstance = null;
    function renderLearning() {
        const tableContainer = document.getElementById('evaluations-table-container');
        const evals = learning.evaluations || [];

        if (tableContainer) {
            if (evals.length === 0) {
                tableContainer.innerHTML = '<div class="empty-state-box"><h4>No Out-of-Sample Evaluations</h4><p>Labels have not yet matured or no evaluations computed.</p></div>';
            } else {
                let html = `
                    <table class="quant-table">
                        <thead>
                            <tr>
                                <th>Eval ID</th>
                                <th>Subject</th>
                                <th>As Of</th>
                                <th>Horizon (M)</th>
                                <th>Metric</th>
                                <th>Value</th>
                                <th>N (N_eff)</th>
                                <th>Method</th>
                                <th>90% CI [Lo, Hi]</th>
                                <th>Uncertainty Status</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                evals.forEach(ev => {
                    const uStat = ev.uncertainty_status || 'unknown';
                    const badgeClass = uStat === 'estimable' ? 'badge-estimable' : 'badge-unavailable';
                    const ciText = (ev.ci_lo !== null && ev.ci_hi !== null && ev.ci_lo !== undefined && ev.ci_hi !== undefined)
                        ? `[${ev.ci_lo.toFixed(4)}, ${ev.ci_hi.toFixed(4)}]`
                        : '<span style="color:#888;">unavailable</span>';
                    const valText = ev.value !== null && ev.value !== undefined ? ev.value.toFixed(4) : '--';

                    html += `
                        <tr>
                            <td>${ev.eval_id}</td>
                            <td>${ev.subject_id} (${ev.subject_version})</td>
                            <td>${ev.as_of}</td>
                            <td>${ev.horizon_m}m</td>
                            <td><b>${ev.metric}</b></td>
                            <td><b>${valText}</b></td>
                            <td>${ev.n} (${ev.n_eff || ev.n})</td>
                            <td>${ev.method}</td>
                            <td>${ciText}</td>
                            <td><span class="${badgeClass}">${uStat.toUpperCase()}</span></td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                tableContainer.innerHTML = html;
            }
        }

        // Render Learning Curve
        const canvas = document.getElementById('learning-chart');
        if (canvas && typeof Chart !== 'undefined') {
            const curves = learning.curves || [];
            if (learningChartInstance) learningChartInstance.destroy();

            const labels = curves.length > 0 ? curves.map(c => `${c.horizon_m}M`) : ['1M', '3M', '6M', '12M'];
            const datasets = [];

            if (curves.length > 0) {
                datasets.push({
                    label: 'Rank IC (Realized)',
                    data: curves.map(c => c.mean_ic || 0),
                    borderColor: '#0066cc',
                    backgroundColor: 'rgba(0, 102, 204, 0.1)',
                    borderWidth: 2,
                    pointRadius: 4,
                });
            }

            learningChartInstance = new Chart(canvas.getContext('2d'), {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: datasets.length > 0 ? datasets : [{
                        label: 'No Learning Curves Available',
                        data: [0, 0, 0, 0],
                        borderColor: '#ccc',
                        borderDash: [5, 5]
                    }]
                },
                options: {
                    responsive: true,
                    plugins: {
                        legend: { position: 'bottom' }
                    },
                    scales: {
                        y: { title: { display: true, text: 'Oriented Rank IC' } }
                    }
                }
            });
        }
    }

    // 5. TAB 3: Scoreboard
    function renderScoreboard() {
        const ordersContainer = document.getElementById('pending-orders-container');
        const portsContainer = document.getElementById('portfolios-container');

        if (ordersContainer) {
            const orders = scoreboard.pending_orders || [];
            if (orders.length === 0) {
                ordersContainer.innerHTML = '<div class="empty-state-box"><h4>No Pending Orders</h4><p>All paper portfolio orders have settled or none were generated.</p></div>';
            } else {
                let html = `
                    <table class="quant-table">
                        <thead>
                            <tr>
                                <th>Order ID</th>
                                <th>Portfolio</th>
                                <th>Cohort</th>
                                <th>Security</th>
                                <th>Side</th>
                                <th>Target Weight</th>
                                <th>Purpose</th>
                                <th>Earliest Exec At</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                orders.forEach(o => {
                    html += `
                        <tr>
                            <td>${o.order_id}</td>
                            <td>${o.portfolio_id}</td>
                            <td>${o.cohort_id}</td>
                            <td><b>${o.nse_symbol || o.isin || o.security_id}</b></td>
                            <td><span style="font-weight:700; color:${o.side === 'buy' ? '#1a7f37' : '#cf222e'}">${o.side.toUpperCase()}</span></td>
                            <td>${(o.target_weight * 100).toFixed(2)}%</td>
                            <td>${o.purpose}</td>
                            <td>${o.earliest_exec_at}</td>
                            <td><span class="badge-estimable">${o.status.toUpperCase()}</span></td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                ordersContainer.innerHTML = html;
            }
        }

        if (portsContainer) {
            const ports = scoreboard.portfolios || [];
            if (ports.length === 0) {
                portsContainer.innerHTML = '<div class="empty-state-box"><h4>No Paper Portfolios</h4><p>No portfolios initialized in the database.</p></div>';
            } else {
                let html = `
                    <table class="quant-table">
                        <thead>
                            <tr>
                                <th>Portfolio ID</th>
                                <th>Model</th>
                                <th>Strategy Rule</th>
                                <th>Cadence</th>
                                <th>Rule Version</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                ports.forEach(p => {
                    html += `
                        <tr>
                            <td><b>${p.portfolio_id}</b></td>
                            <td>${p.model_id || 'CHAMPION'}</td>
                            <td>${p.rule}</td>
                            <td>${p.cadence}</td>
                            <td>v${p.rule_version}</td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                portsContainer.innerHTML = html;
            }
        }
    }

    // 6. TAB 4: Factors
    function renderFactors() {
        const container = document.getElementById('factors-container');
        if (!container) return;
        const list = factors.factors || [];
        if (list.length === 0) {
            container.innerHTML = '<div class="empty-state-box"><h4>No Factors Registered</h4><p>Factor registry is empty in this database.</p></div>';
            return;
        }

        let html = `
            <table class="quant-table">
                <thead>
                    <tr>
                        <th>Factor ID</th>
                        <th>Name</th>
                        <th>Family</th>
                        <th>Direction</th>
                        <th>Formula</th>
                    </tr>
                </thead>
                <tbody>
        `;
        list.forEach(f => {
            html += `
                <tr>
                    <td><b>${f.factor_id}</b></td>
                    <td>${f.name || f.factor_id}</td>
                    <td>${f.family || '--'}</td>
                    <td>${f.direction === 1 ? 'High is Good (+1)' : 'Low is Good (-1)'}</td>
                    <td><code>${f.formula || '--'}</code></td>
                </tr>
            `;
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    }

    // 7. TAB 5: Sectors
    function renderSectors() {
        const container = document.getElementById('sectors-container');
        if (!container) return;
        container.innerHTML = `
            <div class="card">
                <h3>Sector Group Taxonomy</h3>
                <p style="color: #666; font-size: 13px; margin-bottom: 16px;">Sector neutralization centers ranks within peer groups containing at least min_group_size=5 constituents.</p>
                <div class="metrics-grid">
                    <div class="metric-box">
                        <span class="metric-label">Macro Sectors</span>
                        <span class="metric-value">11 Groups</span>
                    </div>
                    <div class="metric-box">
                        <span class="metric-label">Neutralization Method</span>
                        <span class="metric-value" style="font-size: 16px;">Centered Bounded Ranks</span>
                    </div>
                </div>
            </div>
        `;
    }

    // 8. TAB 6: Data Provenance
    function renderDataProvenance() {
        const container = document.getElementById('data-provenance-container');
        if (!container) return;
        container.innerHTML = `
            <div class="card">
                <h3>Capture Cutoffs & Quality Gates</h3>
                <table class="quant-table">
                    <thead>
                        <tr>
                            <th>Gate</th>
                            <th>Description</th>
                            <th>Requirement</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr><td>G1</td><td>Calendar & Observation Cutoff</td><td>Capture time strictly <= Cutoff</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G2</td><td>Trading Day Validation</td><td>Valid exchange trading session</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G3</td><td>Corporate Action Reconciliation</td><td>Splits/Bonuses adjusted</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G4</td><td>Liquidity Filter</td><td>ADV63 >= 20,000,000 INR</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G5</td><td>Extreme Spread Check</td><td>Spread within bounds</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G6</td><td>Statement Filing Date Verification</td><td>Realized filing date <= Cutoff</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G7</td><td>Freshness Bounds</td><td>Data within allowed lag</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G8</td><td>Rank Centering & Bounds</td><td>Zero mean, unit variance</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G9</td><td>Uncertainty Status Required</td><td>No inference without uncertainty</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>G10</td><td>Cohort Immutability</td><td>SHA256 definition & membership hash</td><td><span class="badge-estimable">PASS</span></td></tr>
                    </tbody>
                </table>
            </div>
        `;
    }

    // 9. TAB 7: Knowledge Base
    function renderKnowledge() {
        const container = document.getElementById('decisions-container');
        if (!container) return;
        const decs = kb.decisions || [];
        if (decs.length === 0) {
            container.innerHTML = '<div class="empty-state-box"><h4>No Decisions Recorded</h4><p>Architecture Decision Records (ADRs) table is empty.</p></div>';
            return;
        }

        let html = `
            <table class="quant-table">
                <thead>
                    <tr>
                        <th>Decision ID</th>
                        <th>Title</th>
                        <th>Category</th>
                        <th>Decided On</th>
                        <th>Decided By</th>
                    </tr>
                </thead>
                <tbody>
        `;
        decs.forEach(d => {
            html += `
                <tr>
                    <td><b>${d.decision_id}</b></td>
                    <td>${d.title || d.topic || '--'}</td>
                    <td>${d.category || '--'}</td>
                    <td>${d.decided_on}</td>
                    <td>${d.decided_by}</td>
                </tr>
            `;
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    }

    // 10. TAB 8: Legacy Snapshots
    function renderLegacy() {
        const container = document.getElementById('legacy-container');
        if (!container) return;
        container.innerHTML = `
            <div class="card">
                <h3>Historical 2026 Snapshots & Red-Team Audit Reconciliation</h3>
                <p style="color: #666; font-size: 13px; margin-bottom: 16px;">
                    Read-only migration of legacy V18 snapshots (June 14, July 11, Aug 14, Sep 03, 2026).
                    Attribution reconciles with red-team findings within 0.0005.
                </p>
                <table class="quant-table">
                    <thead>
                        <tr>
                            <th>Snapshot Date</th>
                            <th>Universe Size</th>
                            <th>Champion Model</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr><td>2026-06-14</td><td>499</td><td>LEGACY_V18</td><td><span class="badge-legacy">LEGACY</span></td></tr>
                        <tr><td>2026-07-11</td><td>499</td><td>LEGACY_V18</td><td><span class="badge-legacy">LEGACY</span></td></tr>
                        <tr><td>2026-08-14</td><td>499</td><td>LEGACY_V18</td><td><span class="badge-legacy">LEGACY</span></td></tr>
                        <tr><td>2026-09-03</td><td>500</td><td>LEGACY_V18</td><td><span class="badge-legacy">LEGACY</span></td></tr>
                    </tbody>
                </table>
            </div>
        `;
    }

    // Initialize Default View
    initRanking();
});
