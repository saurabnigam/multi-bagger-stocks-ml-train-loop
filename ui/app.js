// Main initialization
document.addEventListener('DOMContentLoaded', () => {
    
    // Inject AI Weights
    const weightsContainer = document.getElementById('ai-weights-container');
    if (typeof aiWeights !== 'undefined') {
        let weightStr = "<b>Current AI Brain Weights:</b><br/>";
        for (const [k, v] of Object.entries(aiWeights)) {
            weightStr += `<span style="display:inline-block; width:45%; margin-top:2px;">${k}: <b>${v}</b></span>`;
        }
        weightsContainer.innerHTML = weightStr;
    }

    const stockListEl = document.getElementById('stock-list');
    const detailViewEl = document.getElementById('detail-view');
    const tabAccepted = document.getElementById('tab-accepted');
    const tabRejected = document.getElementById('tab-rejected');
    let chartInstance = null;
    let currentTab = 'accepted';
    const tabTurnaround = document.getElementById('tab-turnaround');

    // Tab Listeners
    tabAccepted.addEventListener('click', () => {
        currentTab = 'accepted';
        tabAccepted.style.background = '#0066cc'; tabAccepted.style.color = 'white';
        tabRejected.style.background = '#e5e5ea'; tabRejected.style.color = '#333';
        tabTurnaround.style.background = '#ff9500'; tabTurnaround.style.color = 'white';
        initSidebar();
    });

    tabRejected.addEventListener('click', () => {
        currentTab = 'rejected';
        tabRejected.style.background = '#0066cc'; tabRejected.style.color = 'white';
        tabAccepted.style.background = '#e5e5ea'; tabAccepted.style.color = '#333';
        tabTurnaround.style.background = '#ff9500'; tabTurnaround.style.color = 'white';
        initSidebar();
    });

    tabTurnaround.addEventListener('click', () => {
        currentTab = 'turnaround';
        tabTurnaround.style.background = '#d97d00'; tabTurnaround.style.color = 'white';
        tabAccepted.style.background = '#e5e5ea'; tabAccepted.style.color = '#333';
        tabRejected.style.background = '#e5e5ea'; tabRejected.style.color = '#333';
        initSidebar();
    });

    // Initialize Sidebar
    function initSidebar() {
        stockListEl.innerHTML = '';
        const listToRender = currentTab === 'accepted' ? acceptedStocks : currentTab === 'rejected' ? rejectedStocks : turnaroundStocks;
        
        listToRender.forEach(stock => {
            const li = document.createElement('li');
            li.className = 'stock-item';
            li.dataset.id = stock.id;
            
            let rejectionBadge = '';
            if (currentTab === 'rejected') {
                if (stock.score === 0) {
                    rejectionBadge = '<span style="font-size: 10px; color: white; background: #ff3b30; padding: 2px 4px; border-radius: 4px; margin-left: 6px;">EJECTED</span>';
                } else {
                    rejectionBadge = '<span style="font-size: 10px; color: white; background: #ff9500; padding: 2px 4px; border-radius: 4px; margin-left: 6px;">FAIL</span>';
                }
            } else if (currentTab === 'turnaround') {
                rejectionBadge = '<span style="font-size: 10px; color: white; background: #ff9500; padding: 2px 4px; border-radius: 4px; margin-left: 6px;">CASH BURN</span>';
            }
            
            li.innerHTML = `
                <div class="stock-ticker">${stock.ticker} ${rejectionBadge}</div>
                <div class="stock-name">${stock.name}</div>
            `;
            li.addEventListener('click', () => loadStock(stock));
            stockListEl.appendChild(li);
        });
        
        if (listToRender.length > 0) {
            loadStock(listToRender[0]);
        } else {
            detailViewEl.innerHTML = '<div class="empty-state"><h3>No stocks found in this category.</h3></div>';
        }
    }

    // Load Stock Details
    function loadStock(stock) {
        document.querySelectorAll('.stock-item').forEach(el => el.classList.remove('active'));
        const activeItem = document.querySelector(`.stock-item[data-id="${stock.id}"]`);
        if(activeItem) activeItem.classList.add('active');

        let rejectionBanner = '';
        if (currentTab === 'rejected') {
            rejectionBanner = `
                <div style="background-color: #fff1f0; border-left: 4px solid #ff3b30; padding: 16px; margin-bottom: 24px; border-radius: 4px;">
                    <h3 style="color: #ff3b30; margin: 0 0 8px 0; font-size: 14px;">WHY THIS STOCK WAS REJECTED</h3>
                    <p style="margin: 0; font-size: 14px; font-weight: 500;">${stock.rejection_reason}</p>
                </div>
            `;
        } else if (currentTab === 'turnaround') {
            rejectionBanner = `
                <div style="background-color: #fff8e6; border-left: 4px solid #ff9500; padding: 16px; margin-bottom: 24px; border-radius: 4px;">
                    <h3 style="color: #d97d00; margin: 0 0 8px 0; font-size: 14px;">HYPER-CAPEX / TURNAROUND WARNING</h3>
                    <p style="margin: 0; font-size: 14px; font-weight: 500; line-height: 1.5;">This company was rejected by the main AI because it is burning massive amounts of Free Cash Flow (₹${stock.bearRisk.fcf_burn_raw} Crores last year). However, it has explosive underlying growth (Score: ${stock.plainEnglish.growth}). This is a high-risk Turnaround edge case: if they successfully monetize their CapEx, the stock could explode.</p>
                </div>
            `;
        }

        // Remove empty state class to fix vertical centering bug
        detailViewEl.classList.remove('empty-state');

        // Render HTML
        detailViewEl.innerHTML = `
            <div class="detail-header">
                <h1>${stock.name}</h1>
                <div class="detail-meta">
                    <span class="meta-pill">${stock.ticker}</span>
                    <span class="meta-pill">${stock.sector}</span>
                    <span class="meta-pill">${stock.mcap}</span>
                </div>
            </div>
            
            ${rejectionBanner}

            <div class="dashboard-grid">
                <!-- Left Column -->
                <div class="left-col">
                    <div class="quant-hud">
                        <div class="quant-badge">
                            <span class="hud-label">P/E Ratio</span>
                            <span class="hud-value">${stock.quantTickers ? stock.quantTickers.pe : '--'}x</span>
                        </div>
                        <div class="quant-badge">
                            <span class="hud-label">ROCE</span>
                            <span class="hud-value ${stock.quantTickers && stock.quantTickers.roce > 15 ? 'good' : ''}">${stock.quantTickers ? stock.quantTickers.roce : '--'}%</span>
                        </div>
                        <div class="quant-badge">
                            <span class="hud-label">FCF Yield</span>
                            <span class="hud-value ${stock.quantTickers && stock.quantTickers.fcf_yield > 0 ? 'good' : ''}">${stock.quantTickers ? stock.quantTickers.fcf_yield : '--'}%</span>
                        </div>
                        <div class="quant-badge">
                            <span class="hud-label">Div Yield</span>
                            <span class="hud-value">${stock.quantTickers ? stock.quantTickers.div_yield : '--'}%</span>
                        </div>
                        <div class="quant-badge">
                            <span class="hud-label">50/200 SMA</span>
                            <span class="hud-value ${stock.quantTickers && stock.quantTickers.sma50 > stock.quantTickers.sma200 ? 'good' : ''}">${stock.quantTickers ? stock.quantTickers.sma50 : '--'} / ${stock.quantTickers ? stock.quantTickers.sma200 : '--'}</span>
                        </div>
                        <div class="quant-badge">
                            <span class="hud-label">Inst Holdings</span>
                            <span class="hud-value">${stock.quantTickers ? stock.quantTickers.inst_holdings : '--'}%</span>
                        </div>
                    </div>

                    <div class="card" style="margin-bottom: 24px;">
                        <h3>The Math: Factor Breakdown</h3>
                        <div class="metrics-grid">
                            <div class="metric-box" style="border-left: 4px solid #0066cc; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700;">Fundamental Growth</span>
                                <span class="metric-value" style="font-size: 13px; font-weight: normal; color: #333; margin-top: 4px;">${stock.plainEnglish.growth}</span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #8e8e93; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700;">Momentum Analysis</span>
                                <span class="metric-value" style="font-size: 13px; font-weight: normal; color: #333; margin-top: 4px;">${stock.plainEnglish.momentum}</span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #34c759; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700;">Valuation & Price</span>
                                <span class="metric-value" style="font-size: 13px; font-weight: normal; color: #333; margin-top: 4px;">${stock.plainEnglish.valuation}</span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #af52de; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700;">Smart Money (FII/DII)</span>
                                <span class="metric-value" style="font-size: 13px; font-weight: normal; color: #333; margin-top: 4px;">${stock.plainEnglish.fii}</span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #ff9500; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700;">Balance Sheet Health</span>
                                <span class="metric-value" style="font-size: 13px; font-weight: normal; color: #333; margin-top: 4px;">${stock.plainEnglish.balance_sheet}</span>
                            </div>
                            <div class="metric-box" style="border-left: 4px solid #000000; padding-left: 12px;">
                                <span class="metric-label" style="font-weight: 700;">Business Quality</span>
                                <span class="metric-value" style="font-size: 13px; font-weight: normal; color: #333; margin-top: 4px;">${stock.plainEnglish.quality}</span>
                            </div>
                        </div>
                    </div>

                    <div class="card">
                        <h3>4-Year Cash Flow Trajectory (Crores)</h3>
                        <div class="chart-container">
                            <canvas id="cfChart"></canvas>
                        </div>
                    </div>
                </div>

                <!-- Right Column -->
                <div class="right-col">
                    <div class="card" style="margin-bottom: 24px; background: #e6f7ff; border: 1px solid #91d5ff;">
                        <h3 style="color: #0050b3; display: flex; align-items: center; gap: 8px;">
                            <span>🎙️</span> Concall / Earnings Sentiment
                        </h3>
                        <div style="font-size: 14px; color: #333; margin-top: 8px;">
                            ${stock.plainEnglish.concall}
                        </div>
                    </div>

                    <div class="card" style="margin-bottom: 24px; background: #f8f9fa; border: 1px solid #e9ecef;">
                        <h3 style="color: #000; display: flex; align-items: center; gap: 8px;">
                            <span>📰</span> Latest News Catalyst
                        </h3>
                        <div style="font-size: 16px; font-weight: 500; color: #333; line-height: 1.5;">
                            <a href="${stock.plainEnglish.news_link}" target="_blank" style="color: #0066cc; text-decoration: none;">${stock.plainEnglish.news}</a>
                        </div>
                    </div>

                    <div class="card" style="margin-bottom: 24px;">
                        <h3>Screener Conclusion</h3>
                        <div class="bull-case">
                            ${stock.bullCase}
                        </div>
                    </div>

                    <div class="bear-case-card">
                        <div class="bear-case-header">
                            <span class="bear-case-icon">⚠️</span>
                            <div class="bear-case-title">RED-TEAM ALERT: ${stock.bearRisk.title}</div>
                        </div>
                        <div class="bear-case-desc">
                            ${stock.bearRisk.description}
                        </div>
                        <div style="margin-top: 16px; font-size: 12px; font-weight: 600; color: #ff3b30; text-transform: uppercase;">
                            Risk Level: ${stock.bearRisk.level}
                        </div>
                    </div>
                </div>
            </div>
        `;

        if (stock.cashflows) {
            renderChart(stock.cashflows);
        }
    }

    function renderChart(cashflows) {
        const ctx = document.getElementById('cfChart').getContext('2d');
        
        if (chartInstance) {
            chartInstance.destroy();
        }

        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: ['Year -3', 'Year -2', 'Year -1', 'Latest Year'],
                datasets: [
                    {
                        label: 'Operating Cash Flow (OCF)',
                        data: cashflows.ocf,
                        borderColor: '#0066cc',
                        backgroundColor: 'rgba(0, 102, 204, 0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4
                    },
                    {
                        label: 'Free Cash Flow (FCF)',
                        data: cashflows.fcf,
                        borderColor: '#34c759',
                        backgroundColor: 'transparent',
                        borderWidth: 2,
                        borderDash: [5, 5],
                        tension: 0.4
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            usePointStyle: true,
                            boxWidth: 8
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: {
                            color: 'rgba(0,0,0,0.05)'
                        }
                    },
                    x: {
                        grid: {
                            display: false
                        }
                    }
                }
            }
        });
    }

    // Initialize
    initSidebar();
});
