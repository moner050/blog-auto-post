# 티스토리 애드센스 실전 배치 및 스킨 최적화 가이드

> **작성일자**: 2026-10-02  
> **적용 대상**: 티스토리 블로그 스킨 (HTML / CSS)  
> **기준 문서**: `티스토리_광고_수익_꿀팁.txt` 및 고수익(High-CPC) 최적화 아키텍처

---

## 1. 개요 및 배치 전략

애드센스 수익은 `방문자 수 × 페이지뷰(PV) × 클릭률(CTR) × 클릭당 단가(CPC)`로 결정됩니다.  
본 가이드는 방문자가 포스팅에 진입하여 이탈할 때까지 **시선의 흐름(Eye-tracking)**에 맞춰 광고를 자연스럽게 노출하고, **클릭률(CTR)**을 극대화하는 5대 핵심 배치법을 제공합니다.

```
┌───────────────────────────────────────────────┐
│ [상단] 2열 분할 반응형 배너 (모바일: 1열)    │ ◀ 첫 시선 집중 (고단가 반응형)
├───────────────────────────────────────────────┤
│ 제목 및 요약 안내문                           │
│ [본문 시작] 첫 H2 소제목 직전 인라인 광고      │ ◀ 검색 유입자 솔루션 탐색 직전 (CTR 최다)
├───────────────────────────────────────────────┤
│ 본문 내용 (지식 50% + 행동 50%)              │
│ <blockquote> 신청/조회 안내 박스 </blockquote> │
│ [중간] 솔루션 연계 인피드/디스플레이 광고      │ ◀ 행동 유발(신청/상담) 심리와 결합
├───────────────────────────────────────────────┤
│ [하단] 멀티플렉스 (일치하는 콘텐츠) 광고      │ ◀ 글 종료 시점 이탈 방지
│ <blockquote> 📌 관련 추천 가이드 </blockquote> │ ◀ 내부 링크 체인(PV 2~3배 확장)
└───────────────────────────────────────────────┘
  (우측 사이드바: position: sticky 고정 배너 상시 노출)
```

---

## 2. 구역별 상세 배치 가이드

### 2.1 [구역 1] 본문 상단 2열 반응형 배너 (데스크톱 2열 / 모바일 1열)

방문자가 글을 클릭하고 들어왔을 때 가장 먼저 마주하는 최상단 영역입니다.  
데스크톱에서는 2개의 사각형(300×250 또는 336×280) 배너를 좌우로 나란히 배치하고, 모바일에서는 1개 배너로 자동 전환되도록 반응형 CSS를 적용합니다.

#### HTML 코드 (스킨 편집 > HTML 본문 시작부)
```html
<!-- 애드센스 상단 반응형 2열 그리드 -->
<div class="adsense-top-container">
  <div class="adsense-top-item">
    <!-- 구글 애드센스 단위 코드 1 삽입 -->
    <ins class="adsbygoogle"
         style="display:block"
         data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
         data-ad-slot="1111111111"
         data-ad-format="rectangle"
         data-full-width-responsive="true"></ins>
    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
  </div>
  <div class="adsense-top-item desktop-only">
    <!-- 구글 애드센스 단위 코드 2 삽입 -->
    <ins class="adsbygoogle"
         style="display:block"
         data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
         data-ad-slot="2222222222"
         data-ad-format="rectangle"
         data-full-width-responsive="true"></ins>
    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
  </div>
</div>
```

---

### 2.2 [구역 2] 본문 첫 번째 `<h2>` 직전 인라인 광고 (CTR 최대 지점)

검색 유입자는 제목 바로 밑 서론을 빠르게 훑은 뒤, 자신이 찾는 해결책이 시작되는 **첫 번째 소제목(`<h2>`)**으로 빠르게 스크롤을 내립니다. 이때 소제목 바로 위에 위치한 광고는 자연스러운 시선 정지(Eye-stop) 효과를 유발하여 클릭률이 가장 높게 측정됩니다.

#### 자동 삽입 자바스크립트 (스킨 편집 > HTML `</body>` 직전 추가)
```html
<script>
document.addEventListener("DOMContentLoaded", function() {
  const entryContent = document.querySelector(".entry-content, .article_view, .tt_article_useless_p_margin");
  if (!entryContent) return;

  const firstH2 = entryContent.querySelector("h2");
  if (firstH2) {
    const adContainer = document.createElement("div");
    adContainer.className = "adsense-inline-first-h2";
    adContainer.innerHTML = `
      <div style="margin: 24px 0; text-align: center;">
        <ins class="adsbygoogle"
             style="display:block"
             data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
             data-ad-slot="3333333333"
             data-ad-format="auto"
             data-full-width-responsive="true"></ins>
      </div>
    `;
    firstH2.parentNode.insertBefore(adContainer, firstH2);
    (adsbygoogle = window.adsbygoogle || []).push({});
  }
});
</script>
```

---

### 2.3 [구역 3] 사이드바 Sticky 고정 배너 (체류 시간 광고 수익화)

본문 내용이 2,000~3,000자 이상으로 길어질 때, 본문 중간을 읽는 동안 우측 사이드바가 빈 공간으로 남지 않고 스크롤을 따라 부드럽게 고정되는 배너입니다.

#### CSS 코드 (스킨 편집 > CSS 추가)
```css
/* 사이드바 Sticky 고정 광고 스타일 */
.sidebar .adsense-sticky-sidebar {
  position: -webkit-sticky;
  position: sticky;
  top: 30px; /* 상단 메뉴바 높이에 맞게 조정 */
  z-index: 10;
  margin-top: 20px;
}
```

#### HTML 코드 (스킨 편집 > HTML 사이드바 태그 내부)
```html
<div class="adsense-sticky-sidebar">
  <ins class="adsbygoogle"
       style="display:block"
       data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
       data-ad-slot="4444444444"
       data-ad-format="vertical"
       data-full-width-responsive="true"></ins>
  <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
</div>
```

---

### 2.4 [구역 4] 본문 하단 추천 가이드(내부 링크) 연계 멀티플렉스 광고

자동 포스팅 시스템이 생성하는 하단 `<blockquote>📌 함께 읽으면 도움 되는 관련 추천 가이드</blockquote>` 블록 바로 위에 배치합니다.  
본문을 끝까지 완독한 진성 독자에게 일치하는 콘텐츠(멀티플렉스) 광고를 보여준 후, 바로 아래 추천 가이드 링크를 클릭하여 블로그 내 다른 고수익 글로 이동하게 만듭니다.

#### HTML 코드
```html
<!-- 본문 치환자 [##_article_rep_desc_##] 직후 -->
<div class="adsense-bottom-multiplex" style="margin: 30px 0 15px 0;">
  <ins class="adsbygoogle"
       style="display:block"
       data-ad-format="autorelaxed"
       data-ad-client="ca-pub-XXXXXXXXXXXXXXXX"
       data-ad-slot="5555555555"></ins>
  <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
</div>
```

---

## 3. 통합 반응형 CSS 스니펫 (스킨 `style.css` 하단 추가)

```css
/* ==========================================================================
   구글 애드센스 고수익 최적화 레이아웃 스타일
   ========================================================================== */

/* 상단 2열 반응형 그리드 */
.adsense-top-container {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 16px;
  margin: 20px auto 30px auto;
  max-width: 100%;
}

.adsense-top-item {
  flex: 1;
  min-width: 300px;
  text-align: center;
}

/* 모바일 화면 (화면 너비 768px 이하) 대응 */
@media screen and (max-width: 768px) {
  .adsense-top-container {
    flex-direction: column;
    gap: 12px;
  }
  .desktop-only {
    display: none !important;
  }
}

/* H2 직전 인라인 광고 마진 */
.adsense-inline-first-h2 {
  clear: both;
  margin: 32px 0 24px 0;
}

/* 내부 링크 추천 블록과 하단 광고 간격 */
.entry-content blockquote:last-of-type {
  border-left: 4px solid #2563eb;
  background-color: #f8fafc;
  padding: 16px 20px;
  border-radius: 6px;
  margin-top: 25px;
}
```

---

## 4. 애드센스 정책 준수 및 운영 주의사항

1. **광고 라벨링**: 광고 위에 별도 문구를 적을 경우 반드시 **"광고"** 또는 **"Sponsored Links"**만 사용해야 합니다 ("스폰서 링크 클릭 부탁드립니다" 등 유도 문구는 영구 정지 사유).
2. **모바일 첫 화면 가림 금지**: 모바일 화면 진입 시 본문 첫 문단이 전혀 보이지 않고 광고만 화면을 꽉 채우지 않도록 상단 여백을 유지합니다.
3. **무효 트래픽 방지**: 본인 블로그의 광고를 테스트 목적으로 직접 클릭하는 행위는 절대 금지합니다.
