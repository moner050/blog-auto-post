# AI 주제 추천 리스트 뷰 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 주제 추천 후보를 표 형태로 표시하고 각 후보를 안전하게 삭제할 수 있게 한다.

**Architecture:** 후보 목록 API에 후보 단건 삭제 엔드포인트를 추가한다. 프런트엔드는 기존 카드 HTML 대신 하나의 접근 가능한 표를 렌더링하고, 삭제 확인 후 API를 호출해 목록과 통계를 갱신한다.

**Tech Stack:** FastAPI, SQLAlchemy, vanilla JavaScript, CSS, pytest

## Global Constraints

- 기존 후보 수집, 글 생성, 비공개 발행 대기열 등록 동작은 변경하지 않는다.
- 후보 삭제는 `TopicCandidate`만 삭제하며 연결된 글과 발행 대기열은 유지한다.
- `GENERATING` 후보 삭제는 `409 Conflict`로 거부한다.
- 좁은 화면에서는 후보 표가 가로 스크롤되어야 한다.

---

### Task 1: 후보 삭제 API

**Files:**
- Modify: `app/web/app.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `TopicCandidate`, `TopicCandidateStatus`, `session_factory`
- Produces: `DELETE /api/topic-candidates/{candidate_id}` returning `{ "success": true }`

- [x] **Step 1: Write the failing test**

```python
def test_delete_topic_candidate_removes_only_candidate_record(client):
    candidate_id = create_candidate(client)
    response = client.delete(f"/api/topic-candidates/{candidate_id}")
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert client.get("/api/topic-candidates").json()["count"] == 0

def test_delete_generating_topic_candidate_returns_conflict(client):
    candidate_id = create_candidate(client)
    mark_candidate_generating(candidate_id)
    response = client.delete(f"/api/topic-candidates/{candidate_id}")
    assert response.status_code == 409
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_web.py -k "delete_topic_candidate" -q`

Expected: FAIL because the DELETE route does not exist.

- [x] **Step 3: Write minimal implementation**

```python
@app.delete("/api/topic-candidates/{candidate_id}")
def delete_topic_candidate(candidate_id: int) -> dict[str, bool]:
    with session_factory() as session:
        candidate = session.get(TopicCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="topic candidate not found")
        if candidate.status == TopicCandidateStatus.GENERATING:
            raise HTTPException(status_code=409, detail="topic candidate is generating")
        session.delete(candidate)
        session.commit()
    return {"success": True}
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_web.py -k "delete_topic_candidate" -q`

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add app/web/app.py tests/test_web.py
git commit -m "feat: delete topic candidates"
```

### Task 2: 리스트형 후보 화면과 삭제 동작

**Files:**
- Modify: `app/web/static/js/main.js`
- Modify: `app/web/static/css/style.css`

**Interfaces:**
- Consumes: candidate API response fields `category`, `topic`, `reason`, `sources`, `status`, `article_id`
- Produces: `.topic-candidate-table` and `.btn-delete-candidate` controls

- [x] **Step 1: Write minimal implementation**

```javascript
topicCandidateList.innerHTML = `<div class="topic-candidate-table-scroll"><table class="topic-candidate-table">...</table></div>`;
document.querySelectorAll('.btn-delete-candidate').forEach(button => {
    button.addEventListener('click', event => deleteTopicCandidate(event.currentTarget.dataset.candidateId));
});
```

```javascript
async function deleteTopicCandidate(candidateId) {
    const candidate = currentCandidates.find(item => item.id === Number(candidateId));
    if (!window.confirm(`'${candidate.topic}' 후보를 삭제할까요?`)) return;
    const response = await fetch(`/api/topic-candidates/${candidateId}`, { method: 'DELETE' });
    if (!response.ok) throw new Error((await response.json()).detail || '후보 삭제 실패');
    await Promise.all([fetchTopicCandidates(), fetchStats()]);
}
```

- [x] **Step 2: Run focused checks**

Run: `node --check app/web/static/js/main.js`

Expected: no JavaScript syntax errors.

- [x] **Step 3: Verify in the dashboard**

Open `http://127.0.0.1:9000/` and confirm that the twelve saved candidates appear as table rows, the sources remain links, the action column contains the existing generation/view button and a delete button, and the table becomes horizontally scrollable on a narrow viewport.

- [x] **Step 4: Commit**

```bash
git add app/web/static/js/main.js app/web/static/css/style.css tests/test_web.py
git commit -m "feat: render topic candidates as a list"
```

### Task 3: 전체 검증

**Files:**
- Verify: `app/`, `migrations/`, `tests/`

**Interfaces:**
- Consumes: completed API and client rendering changes
- Produces: verified candidate list and deletion behavior

- [x] **Step 1: Run full verification**

Run: `python -m pytest -q; python -m compileall -q app migrations; node --check app/web/static/js/main.js; git diff --check`

Expected: all tests pass, Python compilation and JavaScript syntax checks succeed, and the diff has no whitespace errors.

- [x] **Step 2: Commit the implementation plan**

```bash
git add docs/superpowers/plans/2026-07-30-topic-candidate-list-view.md
git commit -m "docs: plan topic candidate list view"
```

### Task 4: 한 줄 후보 목록과 상세 모달

**Files:**
- Modify: `app/web/templates/index.html`
- Modify: `app/web/static/js/main.js`
- Modify: `app/web/static/css/style.css`

**Interfaces:**
- Consumes: candidate response fields `id`, `category`, `topic`, `reason`, `sources`, `status`, `article_id`, `error_message`
- Produces: clickable `.topic-candidate-row` and `#modal-topic-candidate`

- [x] **Step 1: Render compact rows and add the modal markup**

```javascript
<tr class="topic-candidate-row" data-candidate-id="${candidate.id}" tabindex="0">
    <td>${category}</td><td>${topic}</td><td>${reason}</td><td>${status}</td>
</tr>
```

```html
<div id="modal-topic-candidate" class="modal hidden">...</div>
```

- [x] **Step 2: Add modal behavior and status-aware actions**

```javascript
function openTopicCandidateModal(candidate) { /* render details and action buttons */ }
```

- [x] **Step 3: Verify dashboard behavior**

Open the dashboard, select a candidate row, verify its complete data and action buttons in the modal, then close the modal without generating or deleting a real candidate.

- [x] **Step 4: Run verification and commit**

Run: `python -m pytest -q; python -m compileall -q app migrations; node --check app/web/static/js/main.js; git diff --check`

Expected: all commands succeed.
