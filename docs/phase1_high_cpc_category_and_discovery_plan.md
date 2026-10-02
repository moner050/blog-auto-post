# 작업 계획서: Phase 1 고수익 카테고리 개편 및 주제 발굴 엔진·UI 동기화

> **작성일자**: 2026-10-02  
> **단계**: Phase 1 (최우선 실행 과제)  
> **목적**: 애드센스 고단가 키워드(법률, 보험, 금융, 차량 등) 중심 6대 카테고리 전면 전환, 주제 발굴(Perplexity) 엔진 개편, 썸네일 매핑 및 관리자 웹 UI 동기화

---

## 1. 개요 및 변경 목표

애드센스 수익률을 극대화하기 위해 기존의 저단가 일상 잡학 위주 카테고리를 폐지하고, 1클릭당 단가(CPC)가 높은 상업적 의도(Commercial Intent) 기반의 6대 카테고리로 전면 전환합니다.

### 7대 고수익 카테고리 정의
1. **`법률·합의·분쟁`**: 로톡 상담 사례, 형사/민사 합의금, 소송/고소 절차, 판례 대처법 (최고 단가 CPC)
2. **`대출·부채·금융`**: 햇살론, 대환대출, 1·2금융 금리 비교, 신용회복, 개인회생
3. **`보험·보상·청구`**: 실손보험 청구, 보험금 부지급 대처, 자동차보험 과실비율, 손해사정 상담 (최고 단가 CPC)
4. **`세금·환급·절세`**: 종합소득세 환급, 양도세/증여세 절세, 삼쩜삼 비교, 숨은 정부지원 환급금
5. **`주식·코인·투자`**: 미국 주식/ETF, 배당주, 비트코인/알트코인 트렌드, 거래소 수수료 비교, 공모주 청약 (최고 단가 CPC)
6. **`차량·리스·렌트`**: 신차 장기렌트/리스 가격 비교, 중고차 시세/침수 점검, 감가상각 및 수리비
7. **`여행·특가·예약`**: 항공권 최저가 비교, 호텔 OTA 프로모션 코드, 환전 및 트래블카드 혜택

---

## 2. 세부 작업 항목 및 체크리스트

| 번호 | 대상 파일 / 모듈 | 세부 작업 내용 | 상태 |
|:---:|:---|:---|:---:|
| **1** | `app/content/thumbnails.py` | `CATEGORY_THUMBNAIL_MAP` 7대 카테고리 매핑 및 키워드 부분 매칭 확장 | **완료** |
| **2** | `storage/` 썸네일 에셋 | 신규 4종(`legal-dispute.png`, `insurance-claim.png`, `car-rental.png`, `stock-crypto.png`) 에셋 생성 및 배치 | **완료** |
| **3** | `app/topics/discovery.py` | `ALLOWED_CATEGORIES` 변경, 고단가 검색 앵글(`RANDOM_SEARCH_ANGLES`) 및 프롬프트 고도화 | **완료** |
| **4** | `app/web/templates/index.html` | 웹 대시보드 포스팅 생성 카테고리 `<select>` 옵션 7종 동기화 | **완료** |
| **5** | `build.gradle` 구성 | 규칙 8에 따른 Gradle 기반 빌드/테스트 환경 구성 | **완료** |
| **6** | `tests/` 단위 테스트 | `test_thumbnails.py`, `test_topic_discovery.py`, `test_topic_discovery_resilience.py` 업데이트 및 전체 1,003개 테스트 100% 통과 검증 | **완료** |

---

## 3. 구현 세부 설계

### 3.1 썸네일 매핑 테이블
```python
CATEGORY_THUMBNAIL_MAP: dict[str, Path] = {
    "법률·합의·분쟁": Path("storage/legal-dispute.png"),
    "대출·부채·금융": Path("storage/loan-finance.png"),
    "보험·보상·청구": Path("storage/insurance-claim.png"),
    "세금·환급·절세": Path("storage/tax-refund.png"),
    "주식·코인·투자": Path("storage/stock-crypto.png"),
    "차량·리스·렌트": Path("storage/car-rental.png"),
    "여행·특가·예약": Path("storage/travel-discount.png"),
}
```

### 3.2 주제 탐색 엔진 프롬프트 앵글 (`discovery.py`)
- 로톡(Lawtalk), 법원 판례, 합의금 산정 기준
- 실손보험/자동차보험 지급 거절 및 금감원 민원 사례
- 대출 규제, 대환대출 최저금리 비교, 주담대 갈아타기
- 장기렌트 vs 리스 견적 비교, 중고차 성능점검 주의점
- 종합소득세 환급, 지방세/국세 환급 신청
- 특가 항공권 및 호텔 예약 프로모션 할인

---

## 4. 검증 기준
- 모든 단위 테스트(`tests/test_thumbnails.py`, `tests/test_topic_discovery*.py` 등)가 오류 없이 성공적으로 통과해야 함.
- Gradle 명령어를 통해 빌드 및 테스트가 정상 구동되어야 함.
