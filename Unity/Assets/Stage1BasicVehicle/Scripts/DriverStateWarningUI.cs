using UnityEngine;
using UnityEngine.UI;

public class DriverStateWarningUI : MonoBehaviour
{
    [Header("References")]
    public UnityTcpVehicleBridge tcpBridge;
    public GameObject warningPanel;
    public Text warningText;
    public Button acknowledgeButton;
    public GameObject emergencyPanel;
    public EmergencyPullOverController emergencyController;

    [Header("Warning Sound")]
    [Range(0f, 1f)] public float warningVolume = 0.25f;
    [Range(200f, 2000f)] public float warningFrequencyHz = 880f;
    [Range(0.05f, 1f)] public float warningDurationSeconds = 0.18f;

    private string previousDriverState = "N";
    private AudioSource audioSource;
    private AudioClip warningClip;
    private Font runtimeFont;

    private void Awake()
    {
        audioSource = gameObject.GetComponent<AudioSource>();
        if (audioSource == null)
        {
            audioSource = gameObject.AddComponent<AudioSource>();
        }
        audioSource.playOnAwake = false;
        audioSource.loop = false;

        runtimeFont = Font.CreateDynamicFontFromOSFont(
            new[]
            {
                "Apple SD Gothic Neo",
                "Arial Unicode MS",
                "Malgun Gothic",
                "Noto Sans CJK KR",
                "Arial"
            },
            32
        );
        if (runtimeFont != null)
        {
            foreach (Text text in GetComponentsInChildren<Text>(true))
            {
                text.font = runtimeFont;
            }
        }

        if (acknowledgeButton != null)
        {
            acknowledgeButton.onClick.AddListener(AcknowledgeWarning);
        }

        SetActive(warningPanel, false);
        SetActive(emergencyPanel, false);
    }

    private void Update()
    {
        string state = tcpBridge == null ? "N" : tcpBridge.CurrentDriverState;
        if (string.IsNullOrEmpty(state))
        {
            state = "N";
        }

        bool emergencyActive = emergencyController != null
            && emergencyController.IsEmergencyActive;
        if (emergencyActive)
        {
            previousDriverState = state;
            SetActive(warningPanel, false);
            SetActive(emergencyPanel, true);
            return;
        }

        if (state != previousDriverState)
        {
            HandleStateEntry(state);
            previousDriverState = state;
        }

        if (state == "N")
        {
            SetActive(warningPanel, false);
            SetActive(emergencyPanel, false);
        }
        else if (state == "E")
        {
            SetActive(warningPanel, false);
            SetActive(emergencyPanel, true);
        }
    }

    private void HandleStateEntry(string state)
    {
        if (state == "D" || state == "A")
        {
            SetActive(emergencyPanel, false);
            if (warningText != null)
            {
                warningText.text = state == "D"
                    ? "졸음운전이 감지되었습니다."
                    : "전방주시 이탈이 감지되었습니다.";
            }
            SetActive(warningPanel, true);
            PlayWarningSound();
        }
        else
        {
            SetActive(warningPanel, false);
            SetActive(emergencyPanel, state == "E");
        }
    }

    public void AcknowledgeWarning()
    {
        // 동일 상태의 20Hz 반복 패킷은 상태 전환이 아니므로 다시 열리지 않습니다.
        SetActive(warningPanel, false);
    }

    private void PlayWarningSound()
    {
        if (audioSource == null || warningVolume <= 0f)
        {
            return;
        }

        if (warningClip == null)
        {
            warningClip = CreateWarningClip();
        }
        audioSource.PlayOneShot(warningClip, warningVolume);
    }

    private AudioClip CreateWarningClip()
    {
        const int sampleRate = 44100;
        int sampleCount = Mathf.Max(1, Mathf.RoundToInt(sampleRate * warningDurationSeconds));
        float[] samples = new float[sampleCount];
        for (int index = 0; index < sampleCount; index++)
        {
            float time = index / (float)sampleRate;
            float envelope = 1f - index / (float)sampleCount;
            samples[index] = Mathf.Sin(2f * Mathf.PI * warningFrequencyHz * time)
                * envelope * 0.35f;
        }

        AudioClip clip = AudioClip.Create(
            "DriverStateWarningBeep",
            sampleCount,
            1,
            sampleRate,
            false
        );
        clip.SetData(samples, 0);
        return clip;
    }

    private static void SetActive(GameObject target, bool active)
    {
        if (target != null && target.activeSelf != active)
        {
            target.SetActive(active);
        }
    }

    private void OnDestroy()
    {
        if (warningClip != null)
        {
            Destroy(warningClip);
        }
        if (runtimeFont != null)
        {
            Destroy(runtimeFont);
        }
    }
}
