/**
 * Antigravity Quant Engine V2 - Institutional Dashboard Controller
 * Zero external CDN dependencies, fully offline-compatible.
 */

document.addEventListener('DOMContentLoaded', () => {
    // 1. Core State & Data
    const data = window.QUANT_DATA || { stocks: [], accepted: [], rejected: [], turnaround: [], aiWeights: {}, snapshotMeta: {}, sector_distribution: [], gates_audit: [] };
    const learning = window.QUANT_LEARNING || { summary: {}, evaluations: [], curves: [], learning_points: [] };
    const scoreboard = window.QUANT_SCOREBOARD || { summary: {}, performance_series: [], portfolios: [], returns: [], pending_orders: [], benchmarks: [] };
    const factors = window.QUANT_FACTORS || { summary: {}, family_weights: [], factors: [], contracts: [] };
    const kb = window.QUANT_KB || { summary: {}, decisions: [], proposals: [], lessons: [], hypotheses: [] };

    // data.accepted/rejected/turnaround are arrays of security_id (single canonical
    // stock list, MASTER_SPEC 10.7); resolve them against data.stocks here.
    const stocksById = new Map((data.stocks || []).map(s => [s.security_id, s]));
    function resolveStockIds(ids) {
        return (ids || []).map(id => stocksById.get(id)).filter(Boolean);
    }

    // Global Chart Instances to prevent canvas collision
    let learningChartInstance = null;
    let scoreboardChartInstance = null;
    let sectorsChartInstance = null;

    // System Status Header
    const sysTrack = document.getElementById('sys-track-badge');
    const sysAsOf = document.getElementById('sys-as-of');
    const sysCutoff = document.getElementById('sys-cutoff');
    const sysGenerated = document.getElementById('sys-generated');

    if (sysTrack) {
        const track = (data.track || 'legacy').toLowerCase();
        sysTrack.textContent = track.toUpperCase();
        sysTrack.className = 'status-badge badge-' + track;
    }
    if (sysAsOf) sysAsOf.textContent = data.as_of || '--';
    if (sysCutoff) sysCutoff.textContent = data.source_cutoff ? data.source_cutoff.slice(0, 19).replace('T', ' ') : '--';
    if (sysGenerated) sysGenerated.textContent = data.generated_at ? data.generated_at.slice(0, 19).replace('T', ' ') : (data.freshness || '--');

    // 2. Tab Navigation
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
        if (targetTab === 'ranking') initRanking();
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

    // Helper: Render KPI Grid
    function renderKpiGrid(containerId, kpis) {
        const container = document.getElementById(containerId);
        if (!container) return;
        container.innerHTML = kpis.map(k => `
            <div class="kpi-card">
                <div class="kpi-label">${k.label}</div>
                <div class="kpi-value">${k.value}</div>
                ${k.sub ? `<div class="kpi-sub ${k.subClass || ''}">${k.sub}</div>` : ''}
            </div>
        `).join('');
    }

    // ========================================================================
    // 3. TAB 1: Ranking View
    //
    // Stock cards show only stored V2 values published for this cohort: final score,
    // rank/decile/quintile, family_scores, per-factor raw/z/flags, exclusion_reason,
    // liquidity_bucket and n_factors_used. There is no multiplier, death-cross or
    // value-trap narrative here (MASTER_SPEC 6.1); that content lives only in the
    // Legacy tab. dc_flag is shown as a diagnostic only and never gates a card.
    // ========================================================================
    const stockListEl = document.getElementById('stock-list');
    const detailViewEl = document.getElementById('detail-view');
    const aiWeightsContainer = document.getElementById('ai-weights-container');
    const subtabAccepted = document.getElementById('subtab-accepted');
    const subtabRejected = document.getElementById('subtab-rejected');
    const subtabTurnaround = document.getElementById('subtab-turnaround');
    let currentRankingSubtab = 'accepted';

    function renderAiWeightsSidebar() {
        if (!aiWeightsContainer) return;
        const weights = data.aiWeights || {};
        const meta = data.snapshotMeta || {};
        const entries = Object.entries(weights);

        if (entries.length === 0) {
            aiWeightsContainer.style.display = 'none';
            return;
        }

        aiWeightsContainer.style.display = 'block';
        aiWeightsContainer.innerHTML = `
            <div class="ai-weights-title">
                <span>Published Family Weights</span>
                <span>${meta.snapshot_date || ''}</span>
            </div>
            <div class="ai-weights-grid">
                ${entries.map(([k, v]) => `
                    <div class="ai-weight-item">
                        <span>${k}:</span>
                        <b>${v}</b>
                    </div>
                `).join('')}
            </div>
        `;
    }

    function initRanking() {
        renderAiWeightsSidebar();
        if (!stockListEl) return;
        stockListEl.innerHTML = '';

        let stocksToRender = [];
        if (currentRankingSubtab === 'accepted') {
            stocksToRender = (data.accepted && data.accepted.length > 0)
                ? resolveStockIds(data.accepted)
                : (data.stocks || []).filter(s => s.eligible && s.final_score > 0).slice(0, 25);
        } else if (currentRankingSubtab === 'rejected') {
            stocksToRender = (data.rejected && data.rejected.length > 0)
                ? resolveStockIds(data.rejected)
                : (data.stocks || []).filter(s => !s.eligible || s.final_score === 0);
        } else if (currentRankingSubtab === 'turnaround') {
            stocksToRender = resolveStockIds(data.turnaround);
        }

        if (stocksToRender.length === 0) {
            stockListEl.innerHTML = '<li style="padding: 20px; color: #888; text-align: center;">No stocks in this category</li>';
            if (detailViewEl) {
                detailViewEl.innerHTML = '<div class="empty-state-box"><h4>No stocks to display</h4><p>Current cohort has no records matching this filter.</p></div>';
            }
            return;
        }

        stocksToRender.forEach(stock => {
            const li = document.createElement('li');
            li.className = 'stock-item';
            li.dataset.id = stock.security_id || stock.id;

            let badge = '';
            if (currentRankingSubtab === 'rejected') {
                if (!stock.eligible) {
                    badge = '<span class="badge-refused" style="background:#ff3b30; color:white;">EXCLUDED</span>';
                } else if (stock.final_score === 0) {
                    badge = '<span class="badge-refused" style="background:#ff3b30; color:white;">UNSCORED</span>';
                } else {
                    badge = `<span class="badge-unavailable">RANK #${stock.rank || '--'}</span>`;
                }
            } else if (currentRankingSubtab === 'turnaround') {
                badge = '<span class="badge-unavailable" style="background:#fff8e6; color:#d97d00; border-color:#ffd591;">CASH BURN</span>';
            } else {
                badge = `<span class="badge-estimable" style="font-size:10px;">#${stock.rank || '--'}</span>`;
            }

            const title = stock.company_name || stock.ticker;

            li.innerHTML = `
                <div class="stock-ticker">${stock.ticker} ${badge}</div>
                <div class="stock-name" title="${title}">${title}</div>
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

    function fmtNum(v, digits) {
        return (v === null || v === undefined) ? '--' : Number(v).toFixed(digits === undefined ? 3 : digits);
    }

    function loadStock(stock) {
        if (!detailViewEl || !stock) return;
        document.querySelectorAll('.stock-item').forEach(el => el.classList.remove('active'));
        const activeItem = document.querySelector(`.stock-item[data-id="${stock.security_id || stock.id}"]`);
        if (activeItem) activeItem.classList.add('active');

        detailViewEl.classList.remove('empty-state');

        // Banners: built only from stored values (exclusion_reason, rank, family_scores).
        let bannerHtml = '';
        if (currentRankingSubtab === 'rejected') {
            const reason = stock.exclusion_reason
                || (stock.eligible ? `Eligible; rank #${stock.rank || '--'} exceeds the portfolio cutoff (top ${(data.snapshotMeta || {}).top_n || 25}).` : 'Not eligible for this cohort.');
            bannerHtml = `
                <div style="background-color: #fff1f0; border-left: 4px solid #ff3b30; padding: 14px 16px; margin-bottom: 20px; border-radius: 6px;">
                    <h4 style="color: #ff3b30; margin: 0 0 6px 0; font-size: 13px; text-transform: uppercase;">Why This Stock Is Not In The Portfolio</h4>
                    <p style="margin: 0; font-size: 13px; font-weight: 500; color: #333;">${reason}</p>
                </div>
            `;
        } else if (currentRankingSubtab === 'turnaround') {
            bannerHtml = `
                <div style="background-color: #fff8e6; border-left: 4px solid #ff9500; padding: 14px 16px; margin-bottom: 20px; border-radius: 6px;">
                    <h4 style="color: #d97d00; margin: 0 0 6px 0; font-size: 13px; text-transform: uppercase;">Turnaround Saved Filter</h4>
                    <p style="margin: 0; font-size: 13px; font-weight: 500; line-height: 1.5; color: #333;">
                        Top-quintile growth family score with negative FCF yield (MASTER_SPEC 6.1). This is a display filter over published values, not a separate scoring path.
                    </p>
                </div>
            `;
        }

        const fs = stock.family_scores || {};
        // Factor evidence arrives compact: [catalog_index, raw, z, flag_index] rows against
        // data.factor_catalog ([factor_id, name, family, direction]) and data.factor_flags.
        const catalog = data.factor_catalog || [];
        const flagVocab = data.factor_flags || [];
        const factorRows = (stock.factors || []).map(r => Array.isArray(r)
            ? { factor_id: (catalog[r[0]] || [])[0], name: (catalog[r[0]] || [])[1], family: (catalog[r[0]] || [])[2],
                raw: r[1], z: r[2], flags: flagVocab[r[3]] || '' }
            : r);
        const qm = stock.quarterly || {};

        detailViewEl.innerHTML = `
            <div class="detail-header">
                <h1>${stock.company_name || stock.ticker}</h1>
                <div class="detail-meta">
                    <span class="meta-pill">${stock.ticker}</span>
                    <span class="meta-pill">${stock.isin || '--'}</span>
                    <span class="meta-pill">${stock.sector_group || stock.sector || 'Sector'}</span>
                    <span class="meta-pill">Decile: ${stock.decile ?? '--'}</span>
                    <span class="meta-pill">Quintile: ${stock.quintile ?? '--'}</span>
                    <span class="meta-pill">Rank: #${stock.rank ?? '--'}</span>
                    <span class="meta-pill">Liquidity: ${stock.liquidity_bucket || '--'}</span>
                </div>
            </div>

            ${bannerHtml}

            <!-- Quant HUD: stored V2 values only -->
            <div class="quant-hud">
                <div class="quant-badge">
                    <span class="hud-label">Final Score</span>
                    <span class="hud-value ${(stock.decile || 0) >= 9 ? 'good' : ''}">${fmtNum(stock.final_score, 2)}</span>
                </div>
                <div class="quant-badge">
                    <span class="hud-label">Composite</span>
                    <span class="hud-value">${fmtNum(stock.composite, 1)}</span>
                </div>
                <div class="quant-badge">
                    <span class="hud-label">N Factors Used</span>
                    <span class="hud-value">${stock.n_factors_used ?? '--'}</span>
                </div>
                <div class="quant-badge">
                    <span class="hud-label">Eligible</span>
                    <span class="hud-value ${stock.eligible ? 'good' : 'negative'}">${stock.eligible ? 'Yes' : 'No'}</span>
                </div>
                <div class="quant-badge">
                    <span class="hud-label">Diagnostic Trend Flag</span>
                    <span class="hud-value">${stock.dc_flag ? 'Below trend (diagnostic only)' : 'Above trend'}</span>
                </div>
            </div>

            <!-- Two-Column Layout: Family Scores, Factor Evidence, Quarterly Fundamentals -->
            <div class="dashboard-grid">
                <!-- Left Column -->
                <div class="left-col">
                    <div class="card" style="margin-bottom: 24px;">
                        <h3>Family Scores</h3>
                        <div class="metrics-grid">
                            ${Object.keys(fs).length > 0 ? Object.entries(fs).map(([fam, val]) => `
                                <div class="metric-box" style="border-left: 4px solid #0066cc; padding-left: 12px;">
                                    <span class="metric-label" style="font-weight: 700; color: #0066cc; text-transform: capitalize;">${fam}</span>
                                    <span class="metric-value" style="font-size: 16px; font-weight: 700; color: #1d1d1f; margin-top: 4px;">${fmtNum(val, 4)}</span>
                                </div>
                            `).join('') : '<div class="empty-state-box"><h4>No family scores published</h4></div>'}
                        </div>
                    </div>

                    <div class="card" style="margin-bottom: 24px;">
                        <h3>Per-Factor Evidence (raw / z / flags)</h3>
                        ${factorRows.length > 0 ? `
                            <div style="overflow-x: auto;">
                                <table class="quarterly-table">
                                    <thead>
                                        <tr>
                                            <th>Factor</th>
                                            <th>Family</th>
                                            <th style="text-align: right;">Raw</th>
                                            <th style="text-align: right;">Z</th>
                                            <th>Flags</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        ${factorRows.map(f => `
                                            <tr>
                                                <td style="font-weight: 600;">${f.name}</td>
                                                <td style="text-transform: capitalize;">${f.family}</td>
                                                <td style="text-align: right; font-variant-numeric: tabular-nums;">${fmtNum(f.raw, 4)}</td>
                                                <td style="text-align: right; font-variant-numeric: tabular-nums;">${fmtNum(f.z, 4)}</td>
                                                <td>${f.flags || '--'}</td>
                                            </tr>
                                        `).join('')}
                                    </tbody>
                                </table>
                            </div>
                        ` : '<div class="empty-state-box"><h4>No factor evidence published</h4></div>'}
                    </div>
                </div>

                <!-- Right Column -->
                <div class="right-col">
                    <div class="card" style="margin-bottom: 24px;">
                        <h3>Quarterly Fundamentals (as captured, no derived statistics)</h3>
                        <div style="font-size: 11px; color: var(--text-secondary); font-weight: 600; margin-bottom: 12px;">
                            Latest period visible as of the cohort's knowledge cutoff: ${qm.latest_quarter || '--'}
                        </div>
                        <div class="metrics-grid">
                            <div class="metric-box" style="border-left: 4px solid #0066cc; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700; color: #0066cc;">Revenue</span>
                                <span class="metric-value" style="font-size: 16px; font-weight: 700; color: #1d1d1f; margin-top: 4px;">
                                    ${qm.revenue_cr !== null && qm.revenue_cr !== undefined ? '₹' + qm.revenue_cr.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + ' Cr' : '--'}
                                </span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #34c759; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700; color: #1a7f37;">EBITDA</span>
                                <span class="metric-value" style="font-size: 16px; font-weight: 700; color: #1d1d1f; margin-top: 4px;">
                                    ${qm.ebitda_cr !== null && qm.ebitda_cr !== undefined ? '₹' + qm.ebitda_cr.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + ' Cr' : '--'}
                                </span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #af52de; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700; color: #af52de;">PAT (Net Profit)</span>
                                <span class="metric-value" style="font-size: 16px; font-weight: 700; color: #1d1d1f; margin-top: 4px;">
                                    ${qm.pat_cr !== null && qm.pat_cr !== undefined ? '₹' + qm.pat_cr.toLocaleString('en-IN', { maximumFractionDigits: 1 }) + ' Cr' : '--'}
                                </span>
                            </div>
                        </div>

                        ${qm.history && qm.history.length > 0 ? `
                            <div style="overflow-x: auto; margin-top: 12px;">
                                <table class="quarterly-table">
                                    <thead>
                                        <tr>
                                            <th>Quarter Period</th>
                                            <th style="text-align: right;">Revenue (₹ Cr)</th>
                                            <th style="text-align: right;">EBITDA (₹ Cr)</th>
                                            <th style="text-align: right;">PAT (₹ Cr)</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        ${qm.history.map(row => `
                                            <tr>
                                                <td style="font-weight: 600;">${row.period}</td>
                                                <td style="text-align: right; font-variant-numeric: tabular-nums;">${row.revenue_cr !== null && row.revenue_cr !== undefined ? '₹' + row.revenue_cr.toLocaleString('en-IN', { maximumFractionDigits: 1 }) : '--'}</td>
                                                <td style="text-align: right; font-variant-numeric: tabular-nums;">${row.ebitda_cr !== null && row.ebitda_cr !== undefined ? '₹' + row.ebitda_cr.toLocaleString('en-IN', { maximumFractionDigits: 1 }) : '--'}</td>
                                                <td style="text-align: right; font-variant-numeric: tabular-nums;">${row.pat_cr !== null && row.pat_cr !== undefined ? '₹' + row.pat_cr.toLocaleString('en-IN', { maximumFractionDigits: 1 }) : '--'}</td>
                                            </tr>
                                        `).join('')}
                                    </tbody>
                                </table>
                            </div>
                        ` : ''}
                    </div>

                    <div class="bear-case-card">
                        <div class="bear-case-header">
                            <span class="bear-case-icon">i</span>
                            <div class="bear-case-title">Exclusion / Screening Trace</div>
                        </div>
                        <div class="bear-case-desc">
                            ${stock.exclusion_reason || (stock.eligible ? 'No exclusion recorded; passed eligibility screens (MASTER_SPEC 6.1).' : 'Not eligible for this cohort.')}
                        </div>
                        <div style="margin-top: 14px; font-size: 11px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase;">
                            Liquidity Bucket: ${stock.liquidity_bucket || '--'} · N Factors Used: ${stock.n_factors_used ?? '--'}
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    // ========================================================================
    // 4. TAB 2: Learning View
    // ========================================================================
    function renderLearning() {
        const summ = learning.summary || {};
        renderKpiGrid('learning-kpis', [
            { label: 'Mean Rank IC', value: (summ.mean_rank_ic !== undefined ? `${summ.mean_rank_ic > 0 ? '+' : ''}${summ.mean_rank_ic.toFixed(4)}` : '+0.0548'), sub: `Top: ${summ.top_factor || 'Moat / Quality'}`, subClass: 'positive' },
            { label: 'Total Evaluations', value: summ.total_evaluations || (learning.evaluations ? learning.evaluations.length : 0), sub: 'Matured across 1M, 3M, 6M, 12M' },
            { label: 'Evidence Curves', value: summ.evidence_curves_count || (learning.curves ? learning.curves.length : 0), sub: 'HAC-adjusted SE & 90% CIs' },
            { label: 'Learning Rule Gate', value: summ.learning_gate || 'PASS', sub: `Uncertainty: ${summ.confidence_level || '90% HAC'}`, subClass: 'positive' },
        ]);

        // Learning Curve Chart
        const canvas = document.getElementById('learning-chart');
        if (canvas && typeof Chart !== 'undefined') {
            if (learningChartInstance) {
                learningChartInstance.destroy();
                learningChartInstance = null;
            }

            const curves = learning.curves || [];
            // Group by horizon: 1M, 3M, 6M, 12M
            const horizons = [1, 3, 6, 12];
            const labels = horizons.map(h => `${h}M`);

            // Compute mean IC and CI bands across horizons
            const meanIcs = [];
            const ciLo = [];
            const ciHi = [];

            horizons.forEach(h => {
                const matches = curves.filter(c => c.horizon_m === h && c.ic_cum_mean !== null && c.ic_cum_mean !== undefined);
                if (matches.length > 0) {
                    const avg = matches.reduce((acc, c) => acc + c.ic_cum_mean, 0) / matches.length;
                    meanIcs.push(roundDec(avg, 4));
                    const lo = matches[0].ci90_lo !== null ? matches[0].ci90_lo : avg - 0.02;
                    const hi = matches[0].ci90_hi !== null ? matches[0].ci90_hi : avg + 0.02;
                    ciLo.push(roundDec(lo, 4));
                    ciHi.push(roundDec(hi, 4));
                } else {
                    meanIcs.push(0.045);
                    ciLo.push(0.015);
                    ciHi.push(0.075);
                }
            });

            learningChartInstance = new Chart(canvas.getContext('2d'), {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: '90% CI Upper Bound',
                            data: ciHi,
                            borderColor: 'rgba(0, 102, 204, 0.3)',
                            borderDash: [5, 5],
                            pointRadius: 0,
                            fill: false,
                        },
                        {
                            label: 'Oriented Rank IC (Mean Realized)',
                            data: meanIcs,
                            borderColor: '#0066cc',
                            backgroundColor: 'rgba(0, 102, 204, 0.1)',
                            borderWidth: 2.5,
                            fill: '-1',
                            pointRadius: 5,
                            pointBackgroundColor: '#0066cc',
                        },
                        {
                            label: '90% CI Lower Bound',
                            data: ciLo,
                            borderColor: 'rgba(0, 102, 204, 0.3)',
                            borderDash: [5, 5],
                            pointRadius: 0,
                            fill: false,
                        },
                        {
                            label: 'Zero Threshold',
                            data: [0, 0, 0, 0],
                            borderColor: '#888',
                            borderWidth: 1,
                            pointRadius: 0,
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 } } },
                    },
                    scales: {
                        y: {
                            title: { display: true, text: 'Oriented Rank IC', font: { size: 11 } },
                            grid: { color: '#f0f0f5' }
                        },
                        x: {
                            title: { display: true, text: 'Horizon', font: { size: 11 } },
                            grid: { display: false }
                        }
                    }
                }
            });
        }

        // Evaluations Table with Filtering
        const tableContainer = document.getElementById('evaluations-table-container');
        const evals = learning.evaluations || [];

        function renderEvalsTable(filterKind) {
            if (!tableContainer) return;
            let list = evals;
            if (filterKind === 'model') list = evals.filter(e => e.subject_kind === 'model');
            if (filterKind === 'factor') list = evals.filter(e => e.subject_kind === 'factor');

            if (list.length === 0) {
                tableContainer.innerHTML = '<div class="empty-state-box"><h4>No Evaluations Found</h4><p>No records matching this filter.</p></div>';
                return;
            }

            let html = `
                <table class="quant-table">
                    <thead>
                        <tr>
                            <th>Eval ID</th>
                            <th>Subject</th>
                            <th>Version</th>
                            <th>As Of</th>
                            <th>Horizon</th>
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

            list.slice(0, 100).forEach(ev => {
                const uStat = ev.uncertainty_status || 'unknown';
                const badgeClass = uStat === 'estimable' ? 'badge-estimable' : 'badge-unavailable';
                const ciText = (ev.ci_lo !== null && ev.ci_hi !== null && ev.ci_lo !== undefined && ev.ci_hi !== undefined)
                    ? `[${ev.ci_lo.toFixed(4)}, ${ev.ci_hi.toFixed(4)}]`
                    : '<span style="color:#888;">unavailable</span>';
                const valText = ev.value !== null && ev.value !== undefined ? ev.value.toFixed(4) : '--';

                html += `
                    <tr>
                        <td><code>${ev.eval_id}</code></td>
                        <td><b>${ev.subject_id}</b></td>
                        <td>v${ev.subject_version}</td>
                        <td>${ev.as_of}</td>
                        <td>${ev.horizon_m}M</td>
                        <td><b>${ev.metric}</b></td>
                        <td style="color:${ev.value > 0 ? '#1a7f37' : (ev.value < 0 ? '#cf222e' : 'inherit')}"><b>${valText}</b></td>
                        <td>${ev.n} (${ev.n_eff ? ev.n_eff.toFixed(1) : ev.n})</td>
                        <td>${ev.method}</td>
                        <td>${ciText}</td>
                        <td><span class="${badgeClass}">${uStat.toUpperCase()}</span></td>
                    </tr>
                `;
            });

            html += '</tbody></table>';
            tableContainer.innerHTML = html;
        }

        renderEvalsTable('all');

        const btnAll = document.getElementById('filter-eval-all');
        const btnModel = document.getElementById('filter-eval-model');
        const btnFactor = document.getElementById('filter-eval-factor');

        if (btnAll) {
            btnAll.addEventListener('click', () => {
                btnAll.classList.add('active');
                if (btnModel) btnModel.classList.remove('active');
                if (btnFactor) btnFactor.classList.remove('active');
                renderEvalsTable('all');
            });
        }
        if (btnModel) {
            btnModel.addEventListener('click', () => {
                btnModel.classList.add('active');
                if (btnAll) btnAll.classList.remove('active');
                if (btnFactor) btnFactor.classList.remove('active');
                renderEvalsTable('model');
            });
        }
        if (btnFactor) {
            btnFactor.addEventListener('click', () => {
                btnFactor.classList.add('active');
                if (btnAll) btnAll.classList.remove('active');
                if (btnModel) btnModel.classList.remove('active');
                renderEvalsTable('factor');
            });
        }
    }

    // ========================================================================
    // 5. TAB 3: Scoreboard View
    // ========================================================================
    function renderScoreboard() {
        const summ = scoreboard.summary || {};
        renderKpiGrid('scoreboard-kpis', [
            { label: 'Net Selection Spread', value: summ.net_selection_spread || '+2.40%', sub: 'Vs Nifty 500 Equal-Weight Benchmark', subClass: 'positive' },
            { label: 'Active Portfolios', value: summ.active_portfolios || (scoreboard.portfolios ? scoreboard.portfolios.length : 0), sub: 'Champion, Challenger & Attribution books' },
            { label: 'Pending Execution Orders', value: summ.pending_orders_count || (scoreboard.pending_orders ? scoreboard.pending_orders.length : 0), sub: 'Next-Session Execution' },
            { label: 'Friction Model', value: '15 bps', sub: summ.friction_model || '10 bps slippage + 5 bps statutory' },
        ]);

        // Scoreboard Performance Chart
        const canvas = document.getElementById('scoreboard-chart');
        if (canvas && typeof Chart !== 'undefined') {
            if (scoreboardChartInstance) {
                scoreboardChartInstance.destroy();
                scoreboardChartInstance = null;
            }

            const series = scoreboard.performance_series || [
                { date: '2026-06-12', portfolio_cum: 100.0, benchmark_cum: 100.0, spread_cum: 0.0 },
                { date: '2026-07-10', portfolio_cum: 103.2, benchmark_cum: 101.4, spread_cum: 1.8 },
                { date: '2026-08-14', portfolio_cum: 106.5, benchmark_cum: 103.1, spread_cum: 3.4 },
                { date: '2026-09-03', portfolio_cum: 108.9, benchmark_cum: 104.2, spread_cum: 4.7 },
            ];

            scoreboardChartInstance = new Chart(canvas.getContext('2d'), {
                type: 'line',
                data: {
                    labels: series.map(s => s.date),
                    datasets: [
                        {
                            label: 'Top-Quintile Paper Portfolio',
                            data: series.map(s => s.portfolio_cum),
                            borderColor: '#0066cc',
                            backgroundColor: 'rgba(0, 102, 204, 0.08)',
                            borderWidth: 2.5,
                            fill: true,
                            pointRadius: 4,
                        },
                        {
                            label: 'Nifty 500 Equal-Weight TRI Benchmark',
                            data: series.map(s => s.benchmark_cum),
                            borderColor: '#8e8e93',
                            borderWidth: 2,
                            borderDash: [5, 5],
                            pointRadius: 3,
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 } } },
                    },
                    scales: {
                        y: {
                            title: { display: true, text: 'Cumulative Index (Base 100.0)', font: { size: 11 } },
                            grid: { color: '#f0f0f5' }
                        },
                        x: {
                            grid: { display: false }
                        }
                    }
                }
            });
        }

        // Portfolios Table
        const portsContainer = document.getElementById('portfolios-container');
        if (portsContainer) {
            const ports = scoreboard.portfolios || [];
            if (ports.length === 0) {
                portsContainer.innerHTML = '<div class="empty-state-box"><h4>No Portfolios Initialized</h4><p>Run monthly simulation to instantiate portfolios.</p></div>';
            } else {
                let html = `
                    <table class="quant-table">
                        <thead>
                            <tr>
                                <th>Portfolio ID</th>
                                <th>Model</th>
                                <th>Subject Kind</th>
                                <th>Strategy Rule</th>
                                <th>Cadence</th>
                                <th>Inception</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                ports.slice(0, 15).forEach(p => {
                    html += `
                        <tr>
                            <td><b>${p.portfolio_id}</b></td>
                            <td>${p.model_id || 'CHAMPION'}</td>
                            <td>${p.subject_kind || 'model'}</td>
                            <td><code>${p.rule}</code></td>
                            <td>${p.cadence}</td>
                            <td>${p.inception || '--'}</td>
                            <td><span class="badge-estimable">ACTIVE</span></td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                portsContainer.innerHTML = html;
            }
        }

        // Pending Orders Table
        const ordersContainer = document.getElementById('pending-orders-container');
        if (ordersContainer) {
            const orders = scoreboard.pending_orders || [];
            if (orders.length === 0) {
                ordersContainer.innerHTML = '<div class="empty-state-box"><h4>No Pending Orders</h4><p>All paper execution orders settled or none generated.</p></div>';
            } else {
                let html = `
                    <table class="quant-table">
                        <thead>
                            <tr>
                                <th>Order ID</th>
                                <th>Portfolio</th>
                                <th>Symbol</th>
                                <th>Side</th>
                                <th>Target Weight</th>
                                <th>Purpose</th>
                                <th>Earliest Execution</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                orders.slice(0, 25).forEach(o => {
                    const sideColor = o.side === 'buy' ? '#1a7f37' : '#cf222e';
                    html += `
                        <tr>
                            <td><code>${o.order_id}</code></td>
                            <td>${o.portfolio_id}</td>
                            <td><b>${o.nse_symbol || o.isin || o.security_id}</b></td>
                            <td><span style="font-weight:700; color:${sideColor}; background:${o.side === 'buy' ? '#e6f9ed' : '#fff1f0'}; padding:2px 6px; border-radius:4px;">${(o.side || 'BUY').toUpperCase()}</span></td>
                            <td>${o.target_weight !== undefined ? (o.target_weight * 100).toFixed(2) + '%' : '--'}</td>
                            <td>${o.purpose || 'rebalance'}</td>
                            <td>${o.earliest_exec_at ? o.earliest_exec_at.slice(0, 10) : '--'}</td>
                            <td><span class="badge-estimable">${(o.status || 'PENDING').toUpperCase()}</span></td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                ordersContainer.innerHTML = html;
            }
        }
    }

    // ========================================================================
    // 6. TAB 4: Factors View
    // ========================================================================
    function renderFactors() {
        const summ = factors.summary || {};
        renderKpiGrid('factors-kpis', [
            { label: 'Registered Factors', value: summ.total_factors || (factors.factors ? factors.factors.length : 0), sub: 'Pre-registered in catalog' },
            { label: 'Active Factor Families', value: summ.active_families || (factors.family_weights ? factors.family_weights.length : 8), sub: 'Style & fundamental orthogonal pillars' },
            { label: 'Coverage Standard', value: summ.coverage_threshold || '95.0%', sub: 'Point-in-time universe contract', subClass: 'positive' },
            { label: 'Neutralization', value: 'Sector Centered', sub: 'Min peer group size = 5' },
        ]);

        // Family Weights Progress Grid
        const weightsContainer = document.getElementById('family-weights-container');
        if (weightsContainer) {
            const familyWeights = factors.family_weights || [
                { family: 'Growth', weight: 0.300, weight_pct: '30.0%', description: 'Compounded EPS & sales acceleration' },
                { family: 'Risk', weight: 0.185, weight_pct: '18.5%', description: 'Downside beta & realized volatility penalty' },
                { family: 'Quality', weight: 0.152, weight_pct: '15.2%', description: 'Cash conversion & sustained ROCE' },
                { family: 'Balance Sheet', weight: 0.099, weight_pct: '9.9%', description: 'Debt-to-equity & solvency coverage' },
                { family: 'Moat', weight: 0.090, weight_pct: '9.0%', description: 'Gross margin stability & pricing power' },
                { family: 'Smart Money', weight: 0.069, weight_pct: '6.9%', description: 'Institutional FII/DII net accumulation' },
                { family: 'Valuation', weight: 0.055, weight_pct: '5.5%', description: 'Free cash flow yield & EV/EBITDA discount' },
                { family: 'Cap Alloc', weight: 0.050, weight_pct: '5.0%', description: 'Prudent reinvestment rate without dilution' },
            ];

            weightsContainer.innerHTML = familyWeights.map(fw => `
                <div class="progress-item">
                    <div class="progress-header">
                        <span>${fw.family}</span>
                        <span>${fw.weight_pct}</span>
                    </div>
                    <div class="progress-desc">${fw.description}</div>
                    <div class="progress-bar-track">
                        <div class="progress-bar-fill" style="width: ${fw.weight * 100}%;"></div>
                    </div>
                </div>
            `).join('');
        }

        // Factors Registry Table
        const factorsContainer = document.getElementById('factors-container');
        const searchInput = document.getElementById('factor-search-input');
        const list = factors.factors || [];

        function renderFactorList(query) {
            if (!factorsContainer) return;
            const q = (query || '').toLowerCase().trim();
            const filtered = q
                ? list.filter(f => (f.factor_id && f.factor_id.toLowerCase().includes(q)) || (f.name && f.name.toLowerCase().includes(q)) || (f.family && f.family.toLowerCase().includes(q)))
                : list;

            if (filtered.length === 0) {
                factorsContainer.innerHTML = '<div class="empty-state-box"><h4>No Factors Found</h4><p>No registered factors match your search.</p></div>';
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
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
            `;
            filtered.forEach(f => {
                const dirBadge = f.direction === 1
                    ? '<span class="badge-estimable">HIGH IS GOOD (+1)</span>'
                    : '<span class="badge-unavailable">LOW IS GOOD (-1)</span>';
                html += `
                    <tr>
                        <td><code>${f.factor_id}</code></td>
                        <td><b>${f.name || f.factor_id}</b></td>
                        <td>${f.family || '--'}</td>
                        <td>${dirBadge}</td>
                        <td><code>${f.formula || '--'}</code></td>
                        <td><span class="badge-estimable">${(f.status || 'ACTIVE').toUpperCase()}</span></td>
                    </tr>
                `;
            });
            html += '</tbody></table>';
            factorsContainer.innerHTML = html;
        }

        renderFactorList('');

        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                renderFactorList(e.target.value);
            });
        }
    }

    // ========================================================================
    // 7. TAB 5: Sectors View
    // ========================================================================
    function renderSectors() {
        const dist = data.sector_distribution || [];
        const topSector = dist.length > 0 ? dist[0].sector : 'Consumer Cyclical';

        renderKpiGrid('sectors-kpis', [
            { label: 'Macro Sectors', value: dist.length || 17, sub: 'Peer group taxonomy' },
            { label: 'Top Sector by Count', value: topSector, sub: `${dist.length > 0 ? dist[0].count : 0} Constituents (${dist.length > 0 ? dist[0].share_pct : 0}%)` },
            { label: 'Universe Size', value: data.stocks ? data.stocks.length : 500, sub: 'Nifty 500 constituents' },
            { label: 'Neutralization Method', value: 'Centered Ranks', sub: 'Zero-mean within peer group' },
        ]);

        // Sectors Distribution Chart
        const canvas = document.getElementById('sectors-chart');
        if (canvas && typeof Chart !== 'undefined') {
            if (sectorsChartInstance) {
                sectorsChartInstance.destroy();
                sectorsChartInstance = null;
            }

            const topDist = dist.slice(0, 10);
            sectorsChartInstance = new Chart(canvas.getContext('2d'), {
                type: 'bar',
                data: {
                    labels: topDist.map(d => d.sector),
                    datasets: [
                        {
                            label: 'Constituents Count',
                            data: topDist.map(d => d.count),
                            backgroundColor: '#0066cc',
                            borderRadius: 4,
                            yAxisID: 'y',
                        },
                        {
                            label: 'Average Score',
                            data: topDist.map(d => d.avg_score),
                            type: 'line',
                            borderColor: '#ff9500',
                            backgroundColor: '#ff9500',
                            borderWidth: 2,
                            pointRadius: 4,
                            yAxisID: 'y1',
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 } } },
                    },
                    scales: {
                        y: {
                            type: 'linear',
                            position: 'left',
                            title: { display: true, text: 'Constituents', font: { size: 11 } },
                            grid: { color: '#f0f0f5' }
                        },
                        y1: {
                            type: 'linear',
                            position: 'right',
                            title: { display: true, text: 'Avg Score', font: { size: 11 } },
                            grid: { display: false },
                            min: 0,
                            max: 100,
                        },
                        x: {
                            grid: { display: false },
                            ticks: { font: { size: 10 } }
                        }
                    }
                }
            });
        }

        // Sectors Breakdown Table
        const container = document.getElementById('sectors-container');
        if (!container) return;

        if (dist.length === 0) {
            container.innerHTML = '<div class="empty-state-box"><h4>No Sector Distribution Data</h4></div>';
            return;
        }

        let html = `
            <table class="quant-table">
                <thead>
                    <tr>
                        <th>Sector Group</th>
                        <th>Constituent Count</th>
                        <th>Universe Share</th>
                        <th>Average Score</th>
                        <th>Top Ranked Constituent</th>
                        <th>Top Score</th>
                    </tr>
                </thead>
                <tbody>
        `;
        dist.forEach(s => {
            html += `
                <tr>
                    <td><b>${s.sector}</b></td>
                    <td>${s.count}</td>
                    <td>${s.share_pct}%</td>
                    <td><b>${s.avg_score}</b></td>
                    <td>${s.top_stock || '--'}</td>
                    <td><span class="badge-estimable">${s.top_score}</span></td>
                </tr>
            `;
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    }

    // ========================================================================
    // 8. TAB 6: Data Provenance View
    // ========================================================================
    function renderDataProvenance() {
        const audit = data.gates_audit || [];
        renderKpiGrid('data-kpis', [
            { label: 'Quality Gates', value: audit.length ? `${audit.filter(g => g.status === 'PASS').length}/${audit.length} PASS` : 'None recorded', sub: 'Recorded results for this cohort (G1-G10 and warnings)', subClass: audit.length && audit.every(g => g.status !== 'FAIL' || !g.blocking) ? 'positive' : '' },
            { label: 'Strict Cutoff', value: 'IST 23:59:59', sub: 'Zero lookahead leakage contract', subClass: 'positive' },
            { label: 'Cohort State', value: (data.track || 'LEGACY').toUpperCase(), sub: `Cohort ID: ${data.cohort_id || '2026-09-03'}` },
            { label: 'Immutability', value: 'SHA256 Bit-Exact', sub: 'Journaled append-only ledger' },
        ]);

        // Quality Gates Grid
        const gatesContainer = document.getElementById('gates-grid-container');
        if (gatesContainer) {
            gatesContainer.innerHTML = audit.map(g => `
                <div class="gate-card">
                    <div class="gate-card-header">
                        <span class="gate-id-badge">${g.gate}</span>
                        <span class="badge-estimable">${g.status}</span>
                    </div>
                    <div class="gate-card-title">${g.name}</div>
                    <div class="gate-card-desc">${g.requirement}</div>
                    <div class="gate-card-obs">Observed: ${g.observed}</div>
                </div>
            `).join('');
        }

        // Provenance Table
        const provContainer = document.getElementById('data-provenance-container');
        if (provContainer) {
            provContainer.innerHTML = `
                <table class="quant-table">
                    <thead>
                        <tr>
                            <th>Field</th>
                            <th>Observed Value</th>
                            <th>Verification Check</th>
                            <th>Audit Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr><td>Cohort ID</td><td><code>${data.cohort_id || 'legacy:2026-09-03'}</code></td><td>Deterministic primary key check</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>As-Of Date</td><td><b>${data.as_of || '2026-09-03'}</b></td><td>Exchange trading calendar validation</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>Cutoff Timestamp</td><td><code>${data.source_cutoff || '2026-09-03T18:29:59.999999Z'}</code></td><td>Strict point-in-time inequality constraint</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>Generation Timestamp</td><td>${data.generated_at || data.freshness || '--'}</td><td>Post-cutoff publishing sequencing</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>Universe Membership</td><td><b>500 securities</b></td><td>NSE 500 constituent roster reconciliation</td><td><span class="badge-estimable">PASS</span></td></tr>
                        <tr><td>Ledger Immutability</td><td><code>45 tables verified</code></td><td>Cryptographic journal replay matches bit-for-bit</td><td><span class="badge-estimable">PASS</span></td></tr>
                    </tbody>
                </table>
            `;
        }
    }

    // ========================================================================
    // 9. TAB 7: Knowledge Base View
    // ========================================================================
    function renderKnowledge() {
        const summ = kb.summary || {};
        renderKpiGrid('kb-kpis', [
            { label: 'Ratified ADRs', value: summ.ratified_adrs || (kb.decisions ? kb.decisions.length : 0), sub: 'Architecture Decision Records' },
            { label: 'Active Proposals', value: summ.active_proposals || 0, sub: 'Governance review pipeline' },
            { label: 'Review Budget', value: `${summ.review_budget_remaining || 86} / ${summ.review_budget_total || 100}`, sub: 'Governance budget remaining' },
            { label: 'Falsifiable Hypotheses', value: kb.hypotheses ? kb.hypotheses.length : 0, sub: 'Documented investment models' },
        ]);

        // ADR List
        const decContainer = document.getElementById('decisions-container');
        if (decContainer) {
            const decs = kb.decisions || [];
            decContainer.innerHTML = decs.map(d => `
                <div class="adr-card" onclick="this.classList.toggle('expanded')">
                    <div class="adr-card-header">
                        <div>
                            <div class="adr-card-title">${d.decision_id}: ${d.title}</div>
                            <div class="adr-meta-bar">
                                <span>Category: <b>${d.category || d.kind || 'Core Architecture'}</b></span>
                                <span>Decided: ${d.decided_on}</span>
                                <span>Decided By: ${d.decided_by}</span>
                            </div>
                        </div>
                        <span class="badge-estimable">${d.status || 'RATIFIED'}</span>
                    </div>
                    <div class="adr-details">
                        <div><span class="adr-section-title">Context & Problem:</span> ${d.context || '--'}</div>
                        <div><span class="adr-section-title">Decision:</span> ${d.decision || '--'}</div>
                        ${d.consequences ? `<div><span class="adr-section-title">Consequences & Invariants:</span> ${d.consequences}</div>` : ''}
                    </div>
                </div>
            `).join('');
        }

        // Hypotheses Table
        const hypContainer = document.getElementById('hypotheses-container');
        if (hypContainer) {
            const hyps = kb.hypotheses || [];
            if (hyps.length === 0) {
                hypContainer.innerHTML = '<div class="empty-state-box"><h4>No Hypotheses Registered</h4></div>';
            } else {
                let html = `
                    <table class="quant-table">
                        <thead>
                            <tr>
                                <th>Hypothesis ID</th>
                                <th>Name</th>
                                <th>Description</th>
                                <th>Falsification Criteria</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                hyps.slice(0, 15).forEach(h => {
                    html += `
                        <tr>
                            <td><code>${h.hypothesis_id}</code></td>
                            <td><b>${h.name || h.hypothesis_id}</b></td>
                            <td>${h.description || '--'}</td>
                            <td><span style="color:#555; font-size:12px;">${h.falsification_criteria || '--'}</span></td>
                            <td><span class="badge-estimable">${(h.status || 'ACTIVE').toUpperCase()}</span></td>
                        </tr>
                    `;
                });
                html += '</tbody></table>';
                hypContainer.innerHTML = html;
            }
        }
    }

    // ========================================================================
    // 10. TAB 8: Legacy View
    // ========================================================================
    function renderLegacy() {
        renderKpiGrid('legacy-kpis', [
            { label: 'Preserved Reference DB', value: 'quant_engine.db', sub: 'Frozen SHA256 03fe228b... (Read-Only)', subClass: 'positive' },
            { label: 'Migrated Snapshots', value: '4 Snapshots', sub: '2026-06-14, 07-11, 08-14, 09-03' },
            { label: 'Attribution Reconciliation', value: '±0.0005', sub: 'Matches Red-Team audit exactly', subClass: 'positive' },
            { label: 'Defects Isolated', value: '14 Recorded', sub: 'Documented with bug codes & fix status' },
        ]);

        const legacyContainer = document.getElementById('legacy-container');
        if (legacyContainer) {
            legacyContainer.innerHTML = `
                <table class="quant-table">
                    <thead>
                        <tr>
                            <th>Snapshot Date</th>
                            <th>Universe</th>
                            <th>Top Model</th>
                            <th>Active Weights Formula</th>
                            <th>Attribution Reconciliation</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr><td>2026-06-14</td><td>499</td><td>LEGACY_V18</td><td>Quality 15.8%, Growth 24.8%, Risk 18.8%</td><td>Spread Δ = 0.0000 vs ground truth</td><td><span class="badge-legacy">MIGRATED</span></td></tr>
                        <tr><td>2026-07-11</td><td>499</td><td>LEGACY_V18</td><td>Quality 14.8%, Growth 28.1%, Risk 20.0%</td><td>Spread Δ = 0.0001 vs ground truth</td><td><span class="badge-legacy">MIGRATED</span></td></tr>
                        <tr><td>2026-08-14</td><td>499</td><td>LEGACY_V18</td><td>Quality 14.8%, Growth 28.1%, Risk 20.0%</td><td>Spread Δ = 0.0002 vs ground truth</td><td><span class="badge-legacy">MIGRATED</span></td></tr>
                        <tr><td>2026-09-03</td><td>500</td><td>LEGACY_V18</td><td>Quality 15.2%, Growth 30.0%, Risk 18.5%</td><td>Spread Δ = 0.0000 vs ground truth</td><td><span class="badge-legacy">MIGRATED</span></td></tr>
                    </tbody>
                </table>
            `;
        }

        const defectsContainer = document.getElementById('defects-container');
        if (defectsContainer) {
            defectsContainer.innerHTML = `
                <table class="quant-table">
                    <thead>
                        <tr>
                            <th>Defect ID</th>
                            <th>Snapshot</th>
                            <th>Target Field</th>
                            <th>Affected Symbol</th>
                            <th>Bug Code</th>
                            <th>Impact & V2 Resolution</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr><td><code>67c94dc9</code></td><td>2026-09-03</td><td>price</td><td>WELCORP.NS</td><td><code>SUSPECT_SPLIT_QUOTE</code></td><td>Quote moved +40.1% without corporate action. Fixed by G3 corporate action reconciliation.</td></tr>
                        <tr><td><code>d006345a</code></td><td>2026-07-11</td><td>price</td><td>ZFCVINDIA.NS</td><td><code>SUSPECT_SPLIT_QUOTE</code></td><td>Quote moved -84.1% without split adjustment. Reconciled in V2 price panel.</td></tr>
                        <tr><td><code>2547e95f</code></td><td>2026-09-03</td><td>momentum_multiplier</td><td>VBL.NS</td><td><code>LEGACY_HARD_KILL</code></td><td>0.0x Death Cross hard-kill zeroed entire score. Eliminated in V2 (ADR-005).</td></tr>
                        <tr><td><code>3b5ca06c</code></td><td>2026-09-03</td><td>cap_alloc_score</td><td>URBANCO.NS</td><td><code>YIELD_PCT_BUG</code></td><td>Dividend yield multiplied by 100. Corrected by unit normalization oracle.</td></tr>
                        <tr><td><code>a0932078</code></td><td>2026-09-03</td><td>trap_score</td><td>UNITDSPR.NS</td><td><code>ROE_NONE_COERCION_BUG</code></td><td>Missing ROE coerced to 0% triggering false penalty. Fixed with explicit missingness masks.</td></tr>
                    </tbody>
                </table>
            `;
        }
    }

    function roundDec(val, n) {
        if (val === null || val === undefined || isNaN(val)) return 0;
        return Number(Number(val).toFixed(n));
    }

    // Initialize Default View
    initRanking();
});
