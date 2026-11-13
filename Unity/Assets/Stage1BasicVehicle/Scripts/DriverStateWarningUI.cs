using UnityEngine;
using UnityEngine.UI;

public class DriverStateWarningUI : MonoBehaviour
{
    [Header("References")]
    public UnityTcpVehicleBridge tcpBridge;
    public GameObject warningPanel;
    public Text warningText;
    public GameObject emergencyPanel;
    public EmergencyPullOverController emergencyController;

    [Header("Warning Sound")]
    [Range(0f, 1f)] public float warningVolume = 0.6f;
    [Range(200f, 2000f)] public float warningFrequencyHz = 880f;
    [Range(0.05f, 1f)] public float warningDurationSeconds = 0.28f;

    private string previousDriverState = "Normal";
    private AudioSource audioSource;
    private AudioClip warningClip;
    private Font runtimeFont;

    private void Awake()
    {
        EnsureAudioListener();

        audioSource = gameObject.GetComponent<AudioSource>();
        if (audioSource == null)
        {
            audioSource = gameObject.AddComponent<AudioSource>();
        }
        audioSource.playOnAwake = false;
        audioSource.loop = false;
        audioSource.spatialBlend = 0f;

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

        if (warningPanel != null)
        {
            // 이전에 생성한 씬에도 확인 버튼이 남아 있지 않도록 숨깁니다.
            Transform legacyButton = warningPanel.transform.Find("AcknowledgeButton");
            if (legacyButton != null)
            {
                legacyButton.gameObject.SetActive(false);
            }

            RectTransform panelRect = warningPanel.GetComponent<RectTransform>();
            if (panelRect != null)
            {
                panelRect.sizeDelta = new Vector2(panelRect.sizeDelta.x, 150f);
            }
        }
        if (warningText != null)
        {
            warningText.rectTransform.anchoredPosition = Vector2.zero;
        }

        SetActive(warningPanel, false);
        SetActive(emergencyPanel, false);
    }

    private static void EnsureAudioListener()
    {
        foreach (AudioListener listener in FindObjectsOfType<AudioListener>())
        {
            if (listener.isActiveAndEnabled)
            {
                return;
            }
        }

        Camera mainCamera = Camera.main;
        if (mainCamera == null)
        {
            return;
        }

        AudioListener cameraListener = mainCamera.GetComponent<AudioListener>();
        if (cameraListener == null)
        {
            mainCamera.gameObject.AddComponent<AudioListener>();
        }
        else
        {
            cameraListener.enabled = true;
        }
    }

    private void Update()
    {
        string state = tcpBridge == null ? "Normal" : tcpBridge.CurrentDriverState;
        if (string.IsNullOrEmpty(state))
        {
            state = "Normal";
        }

        bool emergencyActive = emergencyController != null
            && emergencyController.IsEmergencyActive;
        if (emergencyActive || state == "Danger")
        {
            previousDriverState = state;
            SetActive(warningPanel, false);
            SetActive(emergencyPanel, true);
            return;
        }

        SetActive(emergencyPanel, false);
        if (state == "Caution")
        {
            if (warningText != null)
            {
                warningText.text = "주의: 운전자 상태를 확인하세요.";
            }
            SetActive(warningPanel, true);
            if (previousDriverState != "Caution")
            {
                PlayWarningSound();
            }
        }
        else
        {
            SetActive(warningPanel, false);
        }
        previousDriverState = state;
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
                * envelope * 0.8f;
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
