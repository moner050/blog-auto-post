document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const statTotal = document.getElementById('stat-total');
    const statVerified = document.getElementById('stat-verified');
    const statPending = document.getElementById('stat-pending');
    const statFailed = document.getElementById('stat-failed');
    const systemBlogName = document.getElementById('system-blog-name');
    const badgeModel = document.getElementById('badge-model');

    const formGenerate = document.getElementById('form-generate-article');
    const inputTopic = document.getElementById('input-topic');
    const inputCategory = document.getElementById('input-category');
    const btnSubmitGenerate = document.getElementById('btn-submit-generate');
    const generationMessage = document.getElementById('generation-message');

    const tableArticlesBody = document.getElementById('table-articles-body');
    const btnRefreshList = document.getElementById('btn-refresh-list');
    const btnRunWorker = document.getElementById('btn-run-worker');

    const modalPreview = document.getElementById('modal-preview');
    const modalOverlay = document.getElementById('modal-overlay');
    const btnCloseModal = document.getElementById('btn-close-modal');
    const modalTitle = document.getElementById('modal-title');
    const modalTags = document.getElementById('modal-tags');
    const modalHtmlContent = document.getElementById('modal-html-content');

    // 1. Fetch Stats
    async function fetchStats() {
        try {
            const res = await fetch('/api/stats');
            const data = await res.json();
            statTotal.textContent = data.total_articles;
            statVerified.textContent = data.verified_articles;
            statPending.textContent = data.pending_jobs;
            statFailed.textContent = data.failed_jobs;
            systemBlogName.textContent = `블로그: ${data.blog_name}`;
            badgeModel.textContent = `Model: ${data.model}`;
        } catch (err) {
            console.error('통계 조회 오류:', err);
        }
    }

    // 2. Fetch Articles List
    async function fetchArticles() {
        try {
            tableArticlesBody.innerHTML = '<tr><td colspan="7" class="text-center">데이터를 불러오는 중입니다...</td></tr>';
            const res = await fetch('/api/articles');
            const data = await res.json();

            if (data.length === 0) {
                tableArticlesBody.innerHTML = '<tr><td colspan="7" class="text-center">생성된 포스팅이 없습니다. 위 폼에서 새 글을 생성해 보세요!</td></tr>';
                return;
            }

            tableArticlesBody.innerHTML = data.map(item => {
                const statusBadge = getStatusBadge(item.status, item.last_error_code, item.last_error_message);
                const publishLink = item.result_url 
                    ? `<a href="${item.result_url}" target="_blank" style="color: var(--accent-blue);">[링크 보기]</a>` 
                    : '<span style="color: var(--text-secondary);">-</span>';

                // 발행 완료 건을 포함하여 모든 항목에 재등록 버튼 전면 노출
                const retryBtn = `<button class="btn btn-warning btn-sm btn-retry" data-id="${item.id}" data-title="${escapeHtml(item.title)}">🔄 재등록</button>`;

                return `
                    <tr>
                        <td>${item.id}</td>
                        <td title="${escapeHtml(item.title)}">
                            <strong class="cell-truncate-title">${escapeHtml(item.title)}</strong>
                            ${item.last_error_message ? `<div class="error-text-sm" title="${escapeHtml(item.last_error_message)}">⚠️ 사유: ${escapeHtml(item.last_error_message)}</div>` : ''}
                        </td>
                        <td title="${escapeHtml(item.category || '-')}">${escapeHtml(item.category || '-')}</td>
                        <td>${statusBadge}</td>
                        <td>${item.created_at}</td>
                        <td>${publishLink}</td>
                        <td>
                            <div class="action-buttons">
                                ${retryBtn}
                                <button class="btn btn-secondary btn-sm btn-preview" data-id="${item.id}">
                                    👁️ 미리보기
                                </button>
                                <button class="btn btn-danger btn-sm btn-delete" data-id="${item.id}" data-title="${escapeHtml(item.title)}">
                                    🗑️ 삭제
                                </button>
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');

            // Attach event listeners
            document.querySelectorAll('.btn-preview').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const id = e.currentTarget.getAttribute('data-id');
                    openPreviewModal(id);
                });
            });

            document.querySelectorAll('.btn-delete').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const id = e.currentTarget.getAttribute('data-id');
                    const title = e.currentTarget.getAttribute('data-title');
                    deleteArticle(id, title);
                });
            });

            document.querySelectorAll('.btn-retry').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    const id = e.currentTarget.getAttribute('data-id');
                    const title = e.currentTarget.getAttribute('data-title');
                    retryArticle(id, title);
                });
            });
        } catch (err) {
            console.error('목록 조회 오류:', err);
            tableArticlesBody.innerHTML = '<tr><td colspan="7" class="text-center text-red">목록을 불러오는 중 오류가 발생했습니다.</td></tr>';
        }
    }

    // Helper: Status Badge HTML
    function getStatusBadge(status, errCode, errMsg) {
        switch (status) {
            case 'VERIFIED':
                return '<span class="badge badge-verified">발행완료 (VERIFIED)</span>';
            case 'PUBLISHING':
                return errCode 
                    ? `<span class="badge badge-failed" title="${escapeHtml(errMsg)}">실패 (${escapeHtml(errCode)})</span>` 
                    : '<span class="badge badge-publishing">발행 중 (PUBLISHING)</span>';
            case 'READY_TO_PUBLISH':
                return '<span class="badge badge-ready">발행대기 (READY)</span>';
            default:
                const label = errCode ? `${status} (${errCode})` : status;
                return `<span class="badge badge-failed" title="${escapeHtml(errMsg)}">${escapeHtml(label)}</span>`;
        }
    }

    // Helper: Escape HTML
    function escapeHtml(str) {
        if (!str) return '';
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // 3. Retry Article (발행 완료 및 실패 포스팅 재등록)
    async function retryArticle(id, title) {
        if (!confirm(`"${title}" 포스팅(ID: ${id})을 다시 발행 대기(PENDING) 상태로 전환하여 재등록하시겠습니까?`)) {
            return;
        }

        try {
            const res = await fetch(`/api/articles/${id}/retry`, { method: 'POST' });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || '재등록 설정 실패');

            alert(data.message);
            fetchStats();
            fetchArticles();
        } catch (err) {
            alert(`포스팅 재등록 중 오류 발생: ${err.message}`);
        }
    }

    // 4. Delete Article
    async function deleteArticle(id, title) {
        if (!confirm(`정말 "${title}" 포스팅 데이터(ID: ${id})를 삭제하시겠습니까?\n연관된 작업 큐 레코드도 함께 제거됩니다.`)) {
            return;
        }

        try {
            const res = await fetch(`/api/articles/${id}`, { method: 'DELETE' });
            const data = await res.json();

            if (!res.ok) throw new Error(data.detail || '삭제 실패');

            alert(data.message);
            fetchStats();
            fetchArticles();
        } catch (err) {
            alert(`포스팅 삭제 중 오류 발생: ${err.message}`);
        }
    }

    // 5. Open Preview Modal
    async function openPreviewModal(id) {
        try {
            const res = await fetch(`/api/articles/${id}`);
            if (!res.ok) throw new Error('상세 조회 실패');
            const data = await res.json();

            modalTitle.textContent = data.title;
            let tagsHtml = (data.tags || []).map(t => `<span class="badge badge-ai">#${escapeHtml(t)}</span>`).join(' ');
            
            if (data.last_error_message) {
                tagsHtml += `<div class="alert-message error" style="margin-top: 0.5rem;">⚠️ 발행 실패 원인: [${escapeHtml(data.last_error_code || 'ERROR')}] ${escapeHtml(data.last_error_message)}</div>`;
            }
            modalTags.innerHTML = tagsHtml;
            modalHtmlContent.innerHTML = data.body_html;

            modalPreview.classList.remove('hidden');
        } catch (err) {
            alert('포스팅 상세 정보를 가져오는 데 실패했습니다.');
        }
    }

    // Close Modal
    function closeModal() {
        modalPreview.classList.add('hidden');
    }
    btnCloseModal.addEventListener('click', closeModal);
    modalOverlay.addEventListener('click', closeModal);

    // 6. Submit Form: Generate Article
    formGenerate.addEventListener('submit', async (e) => {
        e.preventDefault();
        const topic = inputTopic.value.trim();
        const category = inputCategory.value.trim();

        if (!topic) return;

        btnSubmitGenerate.disabled = true;
        btnSubmitGenerate.innerHTML = '<span class="btn-icon">⏳</span> AI 생성 중...';
        showMessage('Perplexity Sonar LLM이 실시간 웹 검색을 바탕으로 포스팅을 생성하고 있습니다. 잠시만 기다려 주세요...', 'success');

        try {
            const res = await fetch('/api/articles/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic, category })
            });

            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '생성 실패');

            showMessage(`🎉 성공: "${data.title}" 글이 생성되어 발행 큐에 등록되었습니다!`, 'success');
            inputTopic.value = '';
            fetchStats();
            fetchArticles();
        } catch (err) {
            showMessage(`⚠️ 오류: ${err.message}`, 'error');
        } finally {
            btnSubmitGenerate.disabled = false;
            btnSubmitGenerate.innerHTML = '<span class="btn-icon">✨</span> 포스팅 생성 & 큐 등록';
        }
    });

    // Show Alert Message
    function showMessage(msg, type) {
        generationMessage.textContent = msg;
        generationMessage.className = `alert-message ${type}`;
        generationMessage.classList.remove('hidden');
    }

    // 7. Run Worker
    btnRunWorker.addEventListener('click', async () => {
        btnRunWorker.disabled = true;
        btnRunWorker.innerHTML = '<span class="btn-icon">⏳</span> 워커 실행 중...';

        try {
            const res = await fetch('/api/jobs/run-worker', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '실행 실패');

            alert(data.message);
            fetchStats();
            fetchArticles();
        } catch (err) {
            alert(`발행 워커 실행 중 오류 발생: ${err.message}`);
        } finally {
            btnRunWorker.disabled = false;
            btnRunWorker.innerHTML = '<span class="btn-icon">⚡</span> 발행 워커 1회 즉시 실행';
        }
    });

    // Refresh Button
    btnRefreshList.addEventListener('click', () => {
        fetchStats();
        fetchArticles();
    });

    // Initial Load
    fetchStats();
    fetchArticles();
});
