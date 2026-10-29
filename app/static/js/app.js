document.addEventListener('DOMContentLoaded', function () {
    const askBtn = document.getElementById('askBtn');
    const userQuery = document.getElementById('userQuery');
    const resultsSection = document.getElementById('resultsSection');
    const loadingSection = document.getElementById('loadingSection');
    const loadingText = document.getElementById('loadingText');
    const sqlQueryEl = document.getElementById('sqlQuery');
    const resultsTable = document.getElementById('resultsTable');
    const resultCount = document.getElementById('resultCount');
    const copySqlBtn = document.getElementById('copySql');
    const suggestionChips = document.querySelectorAll('.suggestion-chip');

    const activeDatasetBadge = document.getElementById('activeDatasetBadge');
    const queryDatasetLabel = document.getElementById('queryDatasetLabel');
    const datasetList = document.getElementById('datasetList');
    const datasetStatus = document.getElementById('datasetStatus');
    const uploadBtn = document.getElementById('uploadBtn');
    const importBtn = document.getElementById('importBtn');
    const csvFiles = document.getElementById('csvFiles');
    const datasetName = document.getElementById('datasetName');
    const datasetUrl = document.getElementById('datasetUrl');
    const linkName = document.getElementById('linkName');
    const dropzone = document.getElementById('dropzone');
    const fileList = document.getElementById('fileList');
    const useSampleBtn = document.getElementById('useSampleBtn');

    const llmBadge = document.getElementById('llmBadge');
    const llmKey = document.getElementById('llmKey');
    const llmBaseUrl = document.getElementById('llmBaseUrl');
    const llmModel = document.getElementById('llmModel');
    const saveLlmBtn = document.getElementById('saveLlmBtn');
    const clearLlmBtn = document.getElementById('clearLlmBtn');
    const llmStatus = document.getElementById('llmStatus');

    let datasets = [];
    let activeId = null;
    const SAMPLE_NAME = 'Sample e-commerce data';

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatNumber(value) {
        return Number(value || 0).toLocaleString();
    }

    function showStatus(message, kind, rawHtml) {
        datasetStatus.style.display = 'block';
        datasetStatus.className = 'dataset-status ' + (kind || 'info');
        if (rawHtml) {
            datasetStatus.innerHTML = message;
        } else {
            datasetStatus.textContent = message;
        }
    }

    function hideStatus() {
        datasetStatus.style.display = 'none';
        datasetStatus.innerHTML = '';
    }

    function showLlmStatus(message, kind) {
        llmStatus.style.display = 'block';
        llmStatus.className = 'dataset-status ' + (kind || 'info');
        llmStatus.textContent = message;
    }

    function hideLlmStatus() {
        llmStatus.style.display = 'none';
        llmStatus.textContent = '';
    }

    function applyLlmSettings(settings) {
        if (settings.base_url) {
            llmBaseUrl.value = settings.base_url;
        }
        if (settings.model) {
            llmModel.value = settings.model;
        }
        if (settings.enabled) {
            llmBadge.textContent = 'External API: ' + settings.model;
            llmKey.value = '';
            llmKey.placeholder = 'Saved token: ' + settings.masked_key;
        } else {
            llmBadge.textContent = 'Local Ollama';
            llmKey.placeholder = 'API key / token';
        }
    }

    async function loadLlmSettings() {
        try {
            const response = await fetch('/api/llm/settings');
            if (!response.ok) {
                return;
            }
            applyLlmSettings(await response.json());
        } catch (error) {
            showLlmStatus('Could not load model settings: ' + error.message, 'error');
        }
    }

    async function saveLlmSettings() {
        const apiKey = llmKey.value.trim();
        if (!apiKey) {
            showLlmStatus('Enter the API key or token provided by the authorized user.', 'error');
            return;
        }

        setBusy(saveLlmBtn, true, 'Saving...');
        hideLlmStatus();
        try {
            const response = await fetch('/api/llm/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    api_key: apiKey,
                    base_url: llmBaseUrl.value.trim(),
                    model: llmModel.value.trim()
                })
            });
            const data = await response.json().catch(function () {
                return {};
            });
            if (!response.ok) {
                throw new Error(data.error || 'Could not save the token.');
            }
            applyLlmSettings(data);
            showLlmStatus(
                'Token saved for this session. Questions now use ' + data.model + '.',
                'success'
            );
        } catch (error) {
            showLlmStatus(error.message, 'error');
        } finally {
            setBusy(saveLlmBtn, false);
        }
    }

    async function clearLlmSettings() {
        setBusy(clearLlmBtn, true, 'Switching...');
        hideLlmStatus();
        try {
            const response = await fetch('/api/llm/settings', { method: 'DELETE' });
            const data = await response.json().catch(function () {
                return {};
            });
            if (!response.ok) {
                throw new Error(data.error || 'Could not clear the token.');
            }
            applyLlmSettings(data);
            showLlmStatus('Using the local Ollama model again.', 'success');
        } catch (error) {
            showLlmStatus(error.message, 'error');
        } finally {
            setBusy(clearLlmBtn, false);
        }
    }

    function activeDataset() {
        return datasets.find(function (item) {
            return item.id === activeId;
        }) || null;
    }

    function updateActiveLabels() {
        const current = activeDataset();
        const name = current ? current.name : SAMPLE_NAME;
        activeDatasetBadge.textContent = name;
        queryDatasetLabel.textContent = name;
        queryDatasetLabel.title = current
            ? 'Questions run against ' + name
            : 'Questions run against the built-in sample database';
    }

    function tableSummary(dataset) {
        if (!dataset || !dataset.tables || !dataset.tables.length) {
            return 'No tables';
        }
        return dataset.tables
            .map(function (table) {
                return table.table + ' (' + formatNumber(table.row_count) + ' rows)';
            })
            .join(', ');
    }

    function renderDatasetList() {
        if (!datasets.length) {
            datasetList.innerHTML =
                '<div class="dataset-empty">No datasets yet. Upload a CSV or import a link to get started ' +
                '(currently using the sample e-commerce data).</div>';
            return;
        }

        datasetList.innerHTML = datasets
            .map(function (dataset) {
                const isActive = dataset.id === activeId;
                const source = dataset.source_type.replace(/_/g, ' ');
                return (
                    '<div class="dataset-item' + (isActive ? ' active' : '') + '">' +
                        '<div class="dataset-info">' +
                            '<div class="dataset-name">' +
                                escapeHtml(dataset.name) +
                                '<span class="source-pill">' + escapeHtml(source) + '</span>' +
                            '</div>' +
                            '<div class="dataset-meta">' +
                                formatNumber(dataset.table_count) + ' table' + (dataset.table_count === 1 ? '' : 's') +
                                ' &middot; ' + formatNumber(dataset.total_rows) + ' rows &middot; ' +
                                escapeHtml(tableSummary(dataset)) +
                            '</div>' +
                        '</div>' +
                        '<div class="dataset-actions">' +
                            '<button type="button" class="mini-btn" data-action="preview" data-id="' + dataset.id + '">' +
                                '<i class="fas fa-eye"></i> Preview' +
                            '</button>' +
                            (isActive
                                ? '<span class="active-pill"><i class="fas fa-check"></i> Active</span>'
                                : '<button type="button" class="mini-btn primary" data-action="activate" data-id="' + dataset.id + '">Use</button>') +
                            '<button type="button" class="mini-btn danger" data-action="delete" data-id="' + dataset.id + '" title="Delete dataset">' +
                                '<i class="fas fa-trash"></i>' +
                            '</button>' +
                        '</div>' +
                    '</div>'
                );
            })
            .join('');
    }

    async function loadDatasets() {
        try {
            const response = await fetch('/api/datasets');
            if (!response.ok) {
                return;
            }
            const data = await response.json();
            datasets = data.datasets || [];
            activeId = data.active_id || null;
            renderDatasetList();
            updateActiveLabels();
        } catch (error) {
            showStatus('Could not load your datasets: ' + error.message, 'error');
        }
    }

    function clearResults() {
        resultsSection.style.display = 'none';
        sqlQueryEl.textContent = '';
        resultsTable.querySelector('thead').innerHTML = '';
        resultsTable.querySelector('tbody').innerHTML = '';
        resultCount.textContent = '';
    }

    function renderPreview(dataset, tables) {
        let html = '<div class="preview-head"><strong>' + escapeHtml(dataset.name) + '</strong>' +
            '<span>' + formatNumber(dataset.total_rows) + ' rows in ' +
            formatNumber(dataset.table_count) + ' table(s)</span></div>';

        tables.forEach(function (table) {
            html += '<div class="preview-table">';
            html += '<h4>' + escapeHtml(table.table) + ' <small>' +
                formatNumber(table.row_count) + ' rows</small></h4>';

            const columns = table.sample_columns || [];
            const rows = table.sample_rows || [];
            if (!columns.length) {
                html += '<p class="hint">No columns found.</p></div>';
                return;
            }

            html += '<div class="preview-scroll"><table><thead><tr>';
            columns.forEach(function (column) {
                html += '<th>' + escapeHtml(column) + '</th>';
            });
            html += '</tr></thead><tbody>';
            rows.forEach(function (row) {
                html += '<tr>';
                row.forEach(function (cell) {
                    html += '<td>' + escapeHtml(cell === null ? 'NULL' : cell) + '</td>';
                });
                html += '</tr>';
            });
            html += '</tbody></table></div>';
            if (!rows.length) {
                html += '<p class="hint">This table has no rows.</p>';
            }
            html += '</div>';
        });

        showStatus(html, 'info', true);
    }

    async function previewDataset(id) {
        showStatus('Loading preview...', 'info');
        try {
            const response = await fetch('/api/datasets/' + id + '/overview');
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Preview failed');
            }
            renderPreview(data.dataset, data.tables || []);
        } catch (error) {
            showStatus('Preview failed: ' + error.message, 'error');
        }
    }

    async function activateDataset(id) {
        try {
            const response = await fetch('/api/datasets/' + id + '/activate', {
                method: 'POST'
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Could not switch dataset');
            }
            activeId = data.dataset.id;
            renderDatasetList();
            updateActiveLabels();
            clearResults();
            showStatus('Now asking questions about "' + data.dataset.name + '".', 'success');
        } catch (error) {
            showStatus(error.message, 'error');
        }
    }

    async function deleteDataset(id) {
        const dataset = datasets.find(function (item) {
            return item.id === id;
        });
        if (!dataset) {
            return;
        }
        if (!window.confirm('Delete "' + dataset.name + '"? This removes the uploaded data.')) {
            return;
        }
        try {
            const response = await fetch('/api/datasets/' + id, { method: 'DELETE' });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Delete failed');
            }
            if (activeId === id) {
                activeId = null;
                clearResults();
            }
            hideStatus();
            await loadDatasets();
            showStatus('Dataset deleted.', 'success');
        } catch (error) {
            showStatus(error.message, 'error');
        }
    }

    async function useSampleData() {
        try {
            const response = await fetch('/api/datasets/default', { method: 'POST' });
            if (!response.ok) {
                throw new Error('Could not switch to sample data');
            }
            activeId = null;
            renderDatasetList();
            updateActiveLabels();
            clearResults();
            showStatus('Using the built-in sample e-commerce data.', 'success');
        } catch (error) {
            showStatus(error.message, 'error');
        }
    }

    function setBusy(button, busy, label) {
        button.disabled = busy;
        if (busy) {
            button.dataset.original = button.innerHTML;
            button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + label;
        } else if (button.dataset.original) {
            button.innerHTML = button.dataset.original;
            delete button.dataset.original;
        }
    }

    async function handleDatasetResponse(response) {
        const data = await response.json().catch(function () {
            return {};
        });
        if (!response.ok) {
            throw new Error(data.error || 'The request failed.');
        }
        activeId = data.dataset.id;
        await loadDatasets();
        clearResults();
        showStatus(
            'Loaded "' + data.dataset.name + '" (' + formatNumber(data.dataset.total_rows) +
            ' rows). Ask a question below to analyze it.',
            'success'
        );
    }

    async function uploadFiles() {
        if (!csvFiles.files.length) {
            showStatus('Choose at least one CSV file first.', 'error');
            return;
        }

        const formData = new FormData();
        Array.prototype.forEach.call(csvFiles.files, function (file) {
            formData.append('files', file);
        });
        if (datasetName.value.trim()) {
            formData.append('name', datasetName.value.trim());
        }

        setBusy(uploadBtn, true, 'Uploading...');
        showStatus('Uploading and analyzing your CSV...', 'info');
        try {
            const response = await fetch('/api/datasets/upload', {
                method: 'POST',
                body: formData
            });
            await handleDatasetResponse(response);
            csvFiles.value = '';
            datasetName.value = '';
            fileList.innerHTML = '';
        } catch (error) {
            showStatus(error.message, 'error');
        } finally {
            setBusy(uploadBtn, false);
        }
    }

    async function importFromLink() {
        const url = datasetUrl.value.trim();
        if (!url) {
            showStatus('Paste a dataset link first.', 'error');
            return;
        }

        setBusy(importBtn, true, 'Importing...');
        showStatus('Downloading and analyzing the dataset. Large files may take a minute...', 'info');
        try {
            const response = await fetch('/api/datasets/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: url, name: linkName.value.trim() })
            });
            await handleDatasetResponse(response);
            datasetUrl.value = '';
            linkName.value = '';
        } catch (error) {
            showStatus(error.message, 'error');
        } finally {
            setBusy(importBtn, false);
        }
    }

    function renderFileList() {
        const files = csvFiles.files;
        if (!files.length) {
            fileList.innerHTML = '';
            return;
        }
        fileList.innerHTML = Array.prototype.map
            .call(files, function (file) {
                const sizeMb = (file.size / (1024 * 1024)).toFixed(2);
                return '<span class="file-chip"><i class="fas fa-file-csv"></i> ' +
                    escapeHtml(file.name) + ' <small>' + sizeMb + ' MB</small></span>';
            })
            .join('');
    }

    // Tabs
    document.querySelectorAll('.tab-btn').forEach(function (tab) {
        tab.addEventListener('click', function () {
            document.querySelectorAll('.tab-btn').forEach(function (other) {
                other.classList.toggle('active', other === tab);
            });
            const target = tab.getAttribute('data-tab');
            document.getElementById('uploadPanel').style.display =
                target === 'upload' ? 'block' : 'none';
            document.getElementById('linkPanel').style.display =
                target === 'link' ? 'block' : 'none';
        });
    });

    // Dataset actions
    datasetList.addEventListener('click', function (event) {
        const button = event.target.closest('button[data-action]');
        if (!button) {
            return;
        }
        const id = Number(button.getAttribute('data-id'));
        const action = button.getAttribute('data-action');
        if (action === 'activate') {
            activateDataset(id);
        } else if (action === 'delete') {
            deleteDataset(id);
        } else if (action === 'preview') {
            previewDataset(id);
        }
    });

    useSampleBtn.addEventListener('click', useSampleData);
    uploadBtn.addEventListener('click', uploadFiles);
    importBtn.addEventListener('click', importFromLink);
    saveLlmBtn.addEventListener('click', saveLlmSettings);
    clearLlmBtn.addEventListener('click', clearLlmSettings);
    csvFiles.addEventListener('change', renderFileList);

    dropzone.addEventListener('dragover', function (event) {
        event.preventDefault();
        dropzone.classList.add('dragover');
    });
    dropzone.addEventListener('dragleave', function () {
        dropzone.classList.remove('dragover');
    });
    dropzone.addEventListener('drop', function (event) {
        event.preventDefault();
        dropzone.classList.remove('dragover');
        if (event.dataTransfer && event.dataTransfer.files.length) {
            csvFiles.files = event.dataTransfer.files;
            renderFileList();
        }
    });

    // Query
    suggestionChips.forEach(function (chip) {
        chip.addEventListener('click', function () {
            userQuery.value = this.getAttribute('data-query');
            userQuery.focus();
        });
    });

    copySqlBtn.addEventListener('click', function () {
        const sql = sqlQueryEl.textContent;
        navigator.clipboard.writeText(sql).then(function () {
            const button = copySqlBtn;
            const originalIcon = button.innerHTML;
            button.innerHTML = '<i class="fas fa-check"></i>';
            setTimeout(function () {
                button.innerHTML = originalIcon;
            }, 1500);
        });
    });

    askBtn.addEventListener('click', async function () {
        const query = userQuery.value.trim();

        if (!query) {
            alert('Please enter a question');
            return;
        }

        const current = activeDataset();
        loadingText.textContent = current
            ? 'Asking "' + current.name + '"...'
            : 'Generating SQL with the sample data...';
        loadingSection.style.display = 'block';
        resultsSection.style.display = 'none';
        askBtn.disabled = true;
        askBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i><span>Processing...</span>';

        try {
            const response = await fetch('/api/query', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ query: query })
            });

            const data = await response.json();

            if (response.ok) {
                sqlQueryEl.textContent = data.sql;

                const thead = resultsTable.querySelector('thead');
                const tbody = resultsTable.querySelector('tbody');
                thead.innerHTML = '';
                tbody.innerHTML = '';

                if (data.columns && data.columns.length > 0) {
                    const headerRow = document.createElement('tr');
                    data.columns.forEach(function (col) {
                        const th = document.createElement('th');
                        th.textContent = col;
                        headerRow.appendChild(th);
                    });
                    thead.appendChild(headerRow);

                    data.results.forEach(function (row) {
                        const tr = document.createElement('tr');
                        data.columns.forEach(function (col) {
                            const td = document.createElement('td');
                            td.textContent = row[col] !== null ? row[col] : 'NULL';
                            tr.appendChild(td);
                        });
                        tbody.appendChild(tr);
                    });

                    resultCount.textContent = data.results.length +
                        (data.results.length !== 1 ? ' rows' : ' row') +
                        (data.truncated ? ' (showing first ' + data.results.length + ')' : '');
                } else {
                    resultCount.textContent = '0 rows';
                }

                resultsSection.style.display = 'flex';
                resultsSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            } else {
                alert('Error: ' + data.error);
            }
        } catch (error) {
            alert('Error: ' + error.message);
        } finally {
            loadingSection.style.display = 'none';
            askBtn.disabled = false;
            askBtn.innerHTML = '<i class="fas fa-paper-plane"></i><span>Generate & Run</span>';
        }
    });

    userQuery.addEventListener('keydown', function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            askBtn.click();
        }
    });

    loadDatasets();
    loadLlmSettings();
});
