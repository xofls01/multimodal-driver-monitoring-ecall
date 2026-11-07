/*
 * Arduino Nano: AD8232 ECG + SEN-11574 Pulse Sensor PPG
 *
 * PPG 처리는 업체 보고서(ECG_PPG_DATA_PROCESSING_REPORT.md) 5장 순서를 Nano 안에서 수행한다.
 *   1. 손가락 접촉 판별
 *   2. DC(기저선) 제거 - 보고서 average_dc_estimator 그대로
 *   3. 23탭 대칭 FIR 저역통과 - 보고서 계수 그대로 (100Hz 에서 -3dB 약 3.1Hz, 60Hz 잡음 -56dB)
 *   4. 박동 검출 - 진폭 20 < pp < 1000, 0.25초 미만 간격 무시 (보고서 값)
 *   5. 순시 BPM = 60 / 간격, 40~180 만 채택
 *   6. 첫 5박으로 기준값, 이후 30 BPM 이상 점프는 버리고 칼만 필터(Q=0.1, R=100)
 *
 * 보고서와 다르게 한 부분 (SEN-11574 파형과 아날로그 출력 때문, 시뮬레이션으로 확인)
 *   - 1단계: 보고서는 MAX30102 의 IR 밝기(>= 25000)로 판별하지만 SEN-11574 출력은 항상
 *     중간값(약 512) 근처라 그 방법을 쓸 수 없다. 2초 동안 원신호가 움직이는지와 0/1023 포화 여부로 판별.
 *   - 4단계: 보고서의 영교차 방식은 SEN-11574 파형의 두 번째 봉우리(중복맥)에서도 0 을 넘어서
 *     72 BPM 을 약 140 BPM 으로 두 배로 센다 (시뮬레이션 오차 50~77 BPM).
 *     그래서 직전 박동 진폭의 절반을 임계값으로 쓰고, 직전 간격의 3/5 동안은 다음 박동을 받지 않는다
 *     (SEN-11574 제조사 PulseSensor 방식). 박동 하나를 놓쳐도 간격 기준은 유지해서 절반 속도로 잠기지 않게 했다.
 *   - 6단계: 첫 5박은 칼만 대신 중앙값으로 기준을 잡는다. 칼만(R=100)으로 처음부터 수렴시키면
 *     정상값에 닿는 데 수십 박이 걸린다. 큰 점프가 5번 연속이면(심박이 실제로 크게 바뀜) 다시 시작한다.
 *
 * ECG 심박 (250Hz, Pan-Tompkins 방식을 줄인 것)
 *   60Hz 노치 → 5~15Hz 대역통과 → 미분 → 제곱 → 150ms 이동적분 → 적응 임계값으로 R 피크
 *   → 최근 RR 8개의 중앙값으로 BPM. RR 8개 중 6개 이상이 중앙값 ±15% 안이고
 *   QRS 세기가 잡음보다 충분히 클 때만 '측정 중'. 전극이 없을 때 잡음을 박동으로 세지 않게 하려는 것.
 *   (실측: 전극 없을 때 QRS 세기 1.2 이하, 몸에 붙였을 때 250~470. 기준 5)
 *
 * 융합 심박 (HR_BPM)
 *   둘 다 측정 중이고 차이 10 BPM 이하 → ECG 0.7 + PPG 0.3 가중평균      HR_SRC 3
 *   둘 다 측정 중인데 10 BPM 넘게 다름 → ECG 값 (R 피크가 더 정확)       HR_SRC 4
 *   ECG 만 → ECG (HR_SRC 1),  PPG 만 → PPG (HR_SRC 2),  둘 다 아님 → 0 (HR_SRC 0)
 *
 * 잡음 대책
 *   - Timer1 1kHz 로 일정한 주기 샘플링. PPG 는 10개 평균(100Hz)이라 평균이 앤티에일리어싱 역할을 한다.
 *   - 채널을 바꾼 직후 값은 버리고 다시 읽는다. ATmega328 은 ADC 하나를 채널끼리 같이 써서,
 *     안 그러면 앞 채널(ECG) 전압이 PPG 값에 섞인다.
 *
 * 출력 (115200 8N1)
 *   PLOT_MODE 0 (기본, 라즈베리파이 연동)
 *     ECG:<0..1023>\tPPG:<0..1023>                                250Hz, 기존 펌웨어와 같은 형식
 *     PPG_BPM:<bpm>\tPPG_STATE:<s>\tPPG_PP:<진폭>\tECG_BPM:<bpm>\tECG_STATE:<s>\tHR_BPM:<bpm>\tHR_SRC:<n>\tDROP:<n>
 *       1초마다. STATE 0=센서/전극 신호 없음  1=찾는 중  2=측정 중.  측정 중이 아니면 BPM 은 0
 *       기존 파이썬은 fullmatch 로 ECG/PPG 줄만 받아서 이 줄은 그냥 건너뛴다.
 *   PLOT_MODE 1 (Arduino IDE 시리얼 플로터로 PPG 확인)
 *     PPG_AC:<필터 출력>,Thresh:<임계값>,Beat:<박동 순간 진폭>,BPM:<bpm>,HR:<융합 bpm>   100Hz
 *
 * 배선 (Nano, 5V)
 *   AD8232 OUTPUT → A0,  SEN-11574 보라색(S) → A3 (현재 배선), 빨강 → 5V, 검정 → GND
 *   손가락 끝을 센서 앞면(하트 그림)에 가볍게 댄다. 세게 누르면 혈류가 막혀 신호가 약해진다.
 */
#include <Arduino.h>

#define PLOT_MODE 0

// ---------- 배선 ----------
const uint8_t ECG_PIN = A0;
const uint8_t PPG_PIN = A3;             // 센서 선이 실제로 꽂힌 핀. 선을 옮기면 여기도 바꿈
const uint8_t LED_PIN = LED_BUILTIN;     // 박동마다 깜빡임

// ---------- 샘플링 ----------
const uint16_t BASE_HZ = 1000;           // Timer1 틱
const uint8_t ECG_DIV = 4;               // 4개 평균 → 250Hz (보고서 ECG 250Hz)
const uint8_t PPG_DIV = 10;              // 10개 평균 → 100Hz (보고서 PPG 100Hz)
const uint16_t PPG_HZ = BASE_HZ / PPG_DIV;

// ---------- 업체 알고리즘 상수 (보고서 5장) ----------
const int16_t BEAT_PP_MIN = 20;          // 5.4 박동 진폭 하한
const int16_t BEAT_PP_MAX = 1000;        // 5.4 박동 진폭 상한
const uint16_t REFRACTORY_N = PPG_HZ / 4;  // 5.5 0.25초 미만 간격은 반사파로 무시
const float BPM_MIN = 40.0, BPM_MAX = 180.0;
const uint8_t KF_WARMUP = 5;             // 5.6 첫 5박
const float JUMP_BPM = 30.0;             // 5.6 이 이상 바뀌면 이상치
const float KF_Q = 0.1, KF_R = 100.0;    // 5.6 칼만 필터. Q 를 키우면(예: 1.0) 심박 변화를 더 빨리 따라감

// FIR 계수 (보고서 5.3). 대칭이라 12개로 23탭을 만든다.
const int16_t FIR_COEFFS[12] = {172, 321, 579, 927, 1360, 1858, 2390, 2916, 3391, 3768, 4012, 4096};

// ---------- SEN-11574 용 추가 상수 ----------
const uint16_t CONTACT_WIN_N = 2 * PPG_HZ;  // 접촉 판별 창 2초
const int16_t CONTACT_PP_MIN = 8;           // 창 안 원신호 변화가 이보다 작으면 손가락 없음
const uint16_t RAIL_LO = 3, RAIL_HI = 1020; // 포화로 보는 값
const uint16_t IBI_MIN_N = PPG_HZ * 60 / 180;  // 박동 간격 기준 범위 (180~40 BPM)
const uint16_t IBI_MAX_N = PPG_HZ * 60 / 40;
const uint16_t IBI_INIT_N = PPG_HZ * 6 / 10;   // 처음 간격 기준 0.6초
const uint16_t PULSE_LOST_N = PPG_HZ * 5 / 2;  // 2.5초 동안 박동이 없으면 검출기 초기화
const uint8_t REJECT_RESTART = 5;           // 연속 이상치가 이만큼이면 기준 다시 잡음
const uint16_t BEAT_TIMEOUT_N = 3 * PPG_HZ; // 3초 동안 채택된 박동이 없으면 '찾는 중'

// ---------- ECG 상수 (250Hz) ----------
const uint16_t ECG_HZ = BASE_HZ / ECG_DIV;
const uint8_t MWI_N = 38;                    // 이동적분 150ms
const uint16_t ECG_LEARN_N = 2 * ECG_HZ;     // 처음 2초로 임계값 학습
const uint16_t ECG_REFRACT_N = ECG_HZ * 3 / 10;  // R 피크 뒤 300ms 는 무시 (T 파)
const uint16_t RR_MIN_N = ECG_HZ * 60 / 180, RR_MAX_N = ECG_HZ * 60 / 40;
const uint8_t RR_KEEP = 8, RR_GOOD_MIN = 6;  // RR 8개 중 6개가 중앙값 ±15% 안이면 측정 중
const float RR_TOL = 0.15;
const uint16_t ECG_DECAY_N = 2 * ECG_HZ;     // 2초 동안 R 피크가 없으면 신호 기준을 절반으로
const uint16_t ECG_TIMEOUT_N = 3 * ECG_HZ;
const float SPKI_MIN = 5.0;                  // QRS 세기 하한 (전극 없음 판별)
const float SNR_MIN = 5.0;                   // QRS 세기 / 잡음 세기

// 계수: scipy iirnotch(60, Q=10, fs=250), butter(2, [5, 15], 'band', fs=250, output='sos')
const float NOTCH_B[3] = {0.92976428, -0.11676076, 0.92976428}, NOTCH_A[2] = {-0.11676076, 0.85952856};
const float BP1_B[3] = {0.0133592, 0.0267184, 0.0133592}, BP1_A[2] = {-1.68135386, 0.77989214};
const float BP2_B[3] = {1.0, -2.0, 1.0}, BP2_A[2] = {-1.8795936, 0.89870989};

// ---------- 융합 ----------
const float AGREE_BPM = 10.0;
const float ECG_WEIGHT = 0.7;

enum PpgState : uint8_t { NO_SIGNAL = 0, SEARCHING = 1, MEASURING = 2 };

// ---------- ISR → loop 링 버퍼 (크기는 2의 거듭제곱) ----------
const uint8_t RAW_RING = 64;             // 250Hz (ECG, PPG) 쌍
volatile uint16_t rawEcg[RAW_RING], rawPpg[RAW_RING];
volatile uint8_t rawHead = 0, rawTail = 0;

const uint8_t PPG_RING = 16;             // 100Hz PPG
volatile uint16_t ppgBuf[PPG_RING];
volatile uint8_t ppgHead = 0, ppgTail = 0;

volatile uint16_t dropped = 0;

// ISR 누적값
uint16_t ecgAcc = 0, ppgFastAcc = 0, ppgAcc = 0;
uint8_t ecgPhase = 0, ppgPhase = 0;

static inline uint16_t readSettled(uint8_t pin) {
  analogRead(pin);                       // 채널 전환 직후 값은 버림
  return analogRead(pin);
}

ISR(TIMER1_COMPA_vect) {
  uint16_t e = readSettled(ECG_PIN);
  uint16_t p = readSettled(PPG_PIN);

  ecgAcc += e;
  ppgFastAcc += p;
  if (++ecgPhase >= ECG_DIV) {
    uint8_t next = (rawHead + 1) & (RAW_RING - 1);
    if (next == rawTail) {
      dropped++;
    } else {
      rawEcg[rawHead] = (ecgAcc + ECG_DIV / 2) / ECG_DIV;
      rawPpg[rawHead] = (ppgFastAcc + ECG_DIV / 2) / ECG_DIV;
      rawHead = next;
    }
    ecgAcc = ppgFastAcc = 0;
    ecgPhase = 0;
  }

  ppgAcc += p;
  if (++ppgPhase >= PPG_DIV) {
    uint8_t next = (ppgHead + 1) & (PPG_RING - 1);
    if (next == ppgTail) {
      dropped++;
    } else {
      ppgBuf[ppgHead] = (ppgAcc + PPG_DIV / 2) / PPG_DIV;
      ppgHead = next;
    }
    ppgAcc = 0;
    ppgPhase = 0;
  }
}

// ---------- 2단계: DC 제거 (보고서 5.2) ----------
int32_t dcReg = 0;
bool dcInit = false;

int16_t averageDcEstimator(int32_t *p, uint16_t x) {
  *p += ((((int32_t)x << 15) - *p) >> 4);
  return *p >> 15;
}

// ---------- 3단계: FIR 저역통과 (보고서 5.3) ----------
int16_t cbuf[32];
uint8_t firOffset = 0;

int16_t lowPassFirFilter(int16_t din) {
  cbuf[firOffset] = din;
  int32_t z = (int32_t)FIR_COEFFS[11] * cbuf[(firOffset - 11) & 0x1F];
  for (uint8_t i = 0; i < 11; i++) {
    z += (int32_t)FIR_COEFFS[i] * (cbuf[(firOffset - i) & 0x1F] + cbuf[(firOffset - 22 + i) & 0x1F]);
  }
  firOffset = (firOffset + 1) & 0x1F;
  return z >> 15;
}

// ---------- 5·6단계: BPM + 이상치 + 칼만 (보고서 5.5, 5.6) ----------
uint32_t sampleN = 0;                    // 100Hz 샘플 번호 (박동 간격을 샘플 수로 정확히 잼)
uint32_t lastGoodN = 0;
float kfX = 0, kfP = 0, lastValidBpm = 0;
float warm[KF_WARMUP];
uint8_t accepted = 0, rejects = 0;

void resetBpm() {
  accepted = rejects = 0;
  kfX = kfP = lastValidBpm = 0;
}

float median5(float *v) {
  float s[KF_WARMUP];
  memcpy(s, v, sizeof(s));
  for (uint8_t i = 1; i < KF_WARMUP; i++) {
    for (uint8_t j = i; j > 0 && s[j - 1] > s[j]; j--) {
      float t = s[j];
      s[j] = s[j - 1];
      s[j - 1] = t;
    }
  }
  return s[KF_WARMUP / 2];
}

void acceptBpm(float bpm) {
  if (accepted >= KF_WARMUP && fabs(bpm - lastValidBpm) >= JUMP_BPM) {
    if (++rejects >= REJECT_RESTART) resetBpm();   // 심박이 실제로 크게 바뀐 경우
    return;
  }
  rejects = 0;
  if (accepted < KF_WARMUP) {
    warm[accepted] = bpm;
    if (accepted == KF_WARMUP - 1) {
      kfX = median5(warm);
      kfP = KF_R / KF_WARMUP;
      lastGoodN = sampleN;
    }
  } else {
    kfP += KF_Q;
    float k = kfP / (kfP + KF_R);
    kfX += k * (bpm - kfX);
    kfP *= 1.0 - k;
    lastGoodN = sampleN;
  }
  lastValidBpm = bpm;
  if (accepted < 255) accepted++;
}

// ---------- 4단계: 박동 검출 ----------
int16_t acCur = 0;
int16_t thresh = 0, peak = 0, trough = 0, lastPp = 0;
bool inPulse = false, ampOk = false, prevValid = false, havePulse = false;
bool ibiReset = true;
uint16_t ibiN = IBI_INIT_N;
uint32_t lastPulseN = 0;

void resetDetector() {
  thresh = peak = trough = 0;
  inPulse = ampOk = prevValid = havePulse = false;
  ibiReset = true;
  ibiN = IBI_INIT_N;
  lastPulseN = sampleN;
}

// 반환값: 박동으로 채택한 순간이면 그 진폭, 아니면 0
int16_t detectBeat(uint16_t x) {
  if (!dcInit) {                         // 첫 값으로 DC 를 맞춰 시작 과도응답을 줄임
    dcReg = (int32_t)x << 15;
    dcInit = true;
  }
  int16_t dc = averageDcEstimator(&dcReg, x);
  int16_t y = acCur = lowPassFirFilter((int16_t)x - dc);
  int16_t beat = 0;

  uint32_t n = sampleN - lastPulseN;
  uint32_t refractory = (uint32_t)ibiN * 3 / 5;
  if (refractory < REFRACTORY_N) refractory = REFRACTORY_N;

  if (y < thresh && n > refractory && y < trough) trough = y;
  if (y > thresh && y > peak) peak = y;

  if (y > thresh && !inPulse && n > refractory) {     // 상승하며 임계값을 넘음 = 박동
    inPulse = true;
    bool valid = ampOk;
    if (valid && prevValid && havePulse) {            // 연속한 두 유효 박동의 간격만 사용
      float bpm = 60.0 * PPG_HZ / n;
      if (bpm > BPM_MIN && bpm < BPM_MAX) {
        acceptBpm(bpm);
        beat = lastPp;
      }
    }
    havePulse = true;
    prevValid = valid;
    // 박동 하나를 놓쳐 간격이 1.5배 넘게 늘면 간격 기준은 그대로 둔다 (절반 속도로 잠기는 것 방지)
    if (ibiReset || n * 2 < (uint32_t)ibiN * 3) {
      ibiN = constrain(n, IBI_MIN_N, IBI_MAX_N);
    }
    ibiReset = false;
    lastPulseN = sampleN;
  }
  if (y < thresh && inPulse) {           // 박동이 끝남: 진폭으로 다음 임계값을 정함
    inPulse = false;
    lastPp = peak - trough;
    ampOk = lastPp > BEAT_PP_MIN && lastPp < BEAT_PP_MAX;
    thresh = trough + lastPp / 2;
    peak = trough = thresh;
  }
  if (n > PULSE_LOST_N) resetDetector();
  return beat;
}

void resetPipeline() {
  dcInit = false;
  memset(cbuf, 0, sizeof(cbuf));
  firOffset = 0;
  acCur = lastPp = 0;
  resetDetector();
  resetBpm();
}

// ---------- 1단계: 접촉 판별 (SEN-11574 용) ----------
uint16_t winMin = 1023, winMax = 0, winRail = 0, winCount = 0;
bool contact = false;

void updateContact(uint16_t x) {
  if (x < winMin) winMin = x;
  if (x > winMax) winMax = x;
  if (x <= RAIL_LO || x >= RAIL_HI) winRail++;
  if (++winCount < CONTACT_WIN_N) return;
  bool now = (int16_t)(winMax - winMin) >= CONTACT_PP_MIN && winRail < CONTACT_WIN_N / 2;
  if (now != contact) resetPipeline();   // 보고서: 손가락 없음 → 필터·카운터 초기화, BPM 0
  contact = now;
  winMin = 1023;
  winMax = winCount = winRail = 0;
}

PpgState ppgState() {
  if (!contact) return NO_SIGNAL;
  if (accepted < KF_WARMUP || sampleN - lastGoodN > BEAT_TIMEOUT_N) return SEARCHING;
  return MEASURING;
}

float reportedBpm() {
  return ppgState() == MEASURING ? kfX : 0.0;
}

// ---------- ECG R 피크 검출 ----------
struct Biquad {
  const float *b, *a;
  float z1, z2;
  float step(float x) {                  // 전치 직접형 II
    float y = b[0] * x + z1;
    z1 = b[1] * x - a[0] * y + z2;
    z2 = b[2] * x - a[1] * y;
    return y;
  }
};

Biquad ecgNotch = {NOTCH_B, NOTCH_A, 0, 0};
Biquad ecgBp1 = {BP1_B, BP1_A, 0, 0};
Biquad ecgBp2 = {BP2_B, BP2_A, 0, 0};
float ecgOffset = 0;                     // 첫 값을 빼서 필터가 큰 계단으로 시작하지 않게
bool ecgFirst = true;
float dHist[4];                          // 미분용 지난 값
float mwiBuf[MWI_N];
uint8_t mwiIdx = 0;
float mwiSum = 0;
float mwi1 = 0, mwi2 = 0;                // 직전, 전전 이동적분 값
uint16_t learnN = 0;
float learnMax = 0, learnSum = 0;
float spki = 0, npki = 0;                // 신호/잡음 피크 추정
uint32_t ecgN = 0, lastQrsN = 0, lastDecayN = 0;
bool haveQrs = false;
uint16_t rrBuf[RR_KEEP];
uint8_t rrCount = 0, rrIdx = 0;

void processEcg(uint16_t raw) {
  ecgN++;
  if (ecgFirst) {
    ecgOffset = raw;
    ecgFirst = false;
  }
  float y = ecgBp2.step(ecgBp1.step(ecgNotch.step(raw - ecgOffset)));
  float d = (2 * y + dHist[0] - dHist[2] - 2 * dHist[3]) / 8;
  dHist[3] = dHist[2];
  dHist[2] = dHist[1];
  dHist[1] = dHist[0];
  dHist[0] = y;
  float sq = d * d;
  mwiSum += sq - mwiBuf[mwiIdx];
  mwiBuf[mwiIdx] = sq;
  if (++mwiIdx == MWI_N) {               // 한 바퀴마다 합을 다시 계산 (float 누적 오차 제거)
    mwiIdx = 0;
    mwiSum = 0;
    for (uint8_t i = 0; i < MWI_N; i++) mwiSum += mwiBuf[i];
  }
  float i0 = (mwiSum > 0 ? mwiSum : 0) / MWI_N;

  if (learnN < ECG_LEARN_N) {
    learnN++;
    if (i0 > learnMax) learnMax = i0;
    learnSum += i0;
    if (learnN == ECG_LEARN_N) {
      spki = learnMax / 3;
      npki = learnSum / ECG_LEARN_N / 2;
      lastDecayN = ecgN;
    }
  } else if (mwi1 > mwi2 && mwi1 >= i0) {          // 한 샘플 전이 이동적분의 꼭대기
    float pk = mwi1;
    if (!haveQrs || ecgN - lastQrsN > ECG_REFRACT_N) {
      if (pk > npki + 0.25 * (spki - npki)) {      // R 피크
        spki = 0.125 * pk + 0.875 * spki;
        if (haveQrs) {
          uint32_t rr = ecgN - lastQrsN;
          if (rr >= RR_MIN_N && rr <= RR_MAX_N) {
            rrBuf[rrIdx] = rr;
            rrIdx = (rrIdx + 1) % RR_KEEP;
            if (rrCount < RR_KEEP) rrCount++;
          }
        }
        haveQrs = true;
        lastQrsN = lastDecayN = ecgN;
      } else {
        npki = 0.125 * pk + 0.875 * npki;
      }
    }
  }
  if (learnN >= ECG_LEARN_N && ecgN - lastDecayN > ECG_DECAY_N) {  // 큰 잡음 뒤 기준이 높게 굳지 않게
    spki *= 0.5;
    lastDecayN = ecgN;
  }
  mwi2 = mwi1;
  mwi1 = i0;
}

uint16_t medianRr() {
  uint16_t s[RR_KEEP];
  for (uint8_t i = 0; i < rrCount; i++) s[i] = rrBuf[i];
  for (uint8_t i = 1; i < rrCount; i++) {
    for (uint8_t j = i; j > 0 && s[j - 1] > s[j]; j--) {
      uint16_t t = s[j];
      s[j] = s[j - 1];
      s[j - 1] = t;
    }
  }
  return rrCount ? s[rrCount / 2] : 0;
}

PpgState ecgState() {
  if (learnN < ECG_LEARN_N) return SEARCHING;
  if (spki < SPKI_MIN) return NO_SIGNAL;
  if (rrCount < RR_GOOD_MIN || ecgN - lastQrsN > ECG_TIMEOUT_N || spki < SNR_MIN * npki) return SEARCHING;
  uint16_t m = medianRr();
  uint8_t good = 0;
  for (uint8_t i = 0; i < rrCount; i++) {
    if (fabs((float)rrBuf[i] - m) <= RR_TOL * m) good++;
  }
  return good >= RR_GOOD_MIN ? MEASURING : SEARCHING;
}

float ecgBpm() {
  return ecgState() == MEASURING ? 60.0 * ECG_HZ / medianRr() : 0.0;
}

// ---------- 융합 ----------
uint8_t hrSource = 0;

float fusedBpm() {
  float e = ecgBpm(), p = reportedBpm();
  bool eOk = e > 0, pOk = p > 0;
  if (eOk && pOk) {
    if (fabs(e - p) <= AGREE_BPM) {
      hrSource = 3;
      return ECG_WEIGHT * e + (1 - ECG_WEIGHT) * p;
    }
    hrSource = 4;
    return e;
  }
  hrSource = eOk ? 1 : pOk ? 2 : 0;
  return eOk ? e : pOk ? p : 0.0;
}

// ---------- PPG 한 샘플 처리 ----------
uint32_t ledOffN = 0;

void processPpg(uint16_t x) {
  sampleN++;
  updateContact(x);
  int16_t beat = contact ? detectBeat(x) : 0;
  if (beat) {
    digitalWrite(LED_PIN, HIGH);
    ledOffN = sampleN + PPG_HZ / 20;     // 50ms
  }
  if (sampleN == ledOffN) digitalWrite(LED_PIN, LOW);

#if PLOT_MODE
  Serial.print(F("PPG_AC:"));
  Serial.print(contact ? acCur : 0);
  Serial.print(F(",Thresh:"));
  Serial.print(contact ? thresh : 0);
  Serial.print(F(",Beat:"));
  Serial.print(beat);
  Serial.print(F(",BPM:"));
  Serial.print(reportedBpm(), 1);
  Serial.print(F(",HR:"));
  Serial.println(fusedBpm(), 1);
#endif
}

void sendStatus() {
  noInterrupts();
  uint16_t d = dropped;
  interrupts();
  float hr = fusedBpm();
  Serial.print(F("PPG_BPM:"));
  Serial.print(reportedBpm(), 1);
  Serial.print(F("\tPPG_STATE:"));
  Serial.print((uint8_t)ppgState());
  Serial.print(F("\tPPG_PP:"));
  Serial.print(lastPp);
  Serial.print(F("\tECG_BPM:"));
  Serial.print(ecgBpm(), 1);
  Serial.print(F("\tECG_STATE:"));
  Serial.print((uint8_t)ecgState());
  Serial.print(F("\tHR_BPM:"));
  Serial.print(hr, 1);
  Serial.print(F("\tHR_SRC:"));
  Serial.print(hrSource);
  Serial.print(F("\tDROP:"));
  Serial.println(d);
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  Serial.begin(115200);

  // ADC 분주 64 (250kHz): 변환 약 56us. 1kHz 틱마다 4번 읽어도 ISR 이 0.25ms 안에 끝난다.
  ADCSRA = (ADCSRA & ~0x07) | _BV(ADPS2) | _BV(ADPS1);
  analogRead(ECG_PIN);

  // Timer1 CTC 1kHz: 16MHz / 64 / 1000
  noInterrupts();
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;
  OCR1A = F_CPU / 64 / BASE_HZ - 1;
  TCCR1B = _BV(WGM12) | _BV(CS11) | _BV(CS10);
  TIMSK1 = _BV(OCIE1A);
  interrupts();
}

uint32_t lastStatusN = 0;

void loop() {
  while (ppgTail != ppgHead) {
    uint16_t x = ppgBuf[ppgTail];
    ppgTail = (ppgTail + 1) & (PPG_RING - 1);
    processPpg(x);
  }

  while (rawTail != rawHead) {
    uint16_t e = rawEcg[rawTail], p = rawPpg[rawTail];
    rawTail = (rawTail + 1) & (RAW_RING - 1);
    processEcg(e);
#if !PLOT_MODE                           // 플롯 모드에서는 250Hz 원신호를 보내지 않음
    Serial.print(F("ECG:"));
    Serial.print(e);
    Serial.print('\t');
    Serial.print(F("PPG:"));
    Serial.println(p);
#else
    (void)p;
#endif
  }

#if !PLOT_MODE
  if (sampleN - lastStatusN >= PPG_HZ) {
    lastStatusN = sampleN;
    sendStatus();
  }
#endif
}
