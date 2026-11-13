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
    private const float WarningRepeatIntervalSeconds = 0.5f;
    private const int TextRenderScale = 2;
    private float nextWarningTime;
    private AudioSource audioSource;
    private AudioClip warningClip;
    private Font runtimeFont;
    private RectTransform assistNotice;
    private CanvasGroup assistNoticeGroup;
    private Text assistNoticeText;
    private Texture2D assistNoticeTexture;
    private Sprite assistNoticeSprite;
    private Texture2D noticeOutlineTexture;
    private Sprite noticeOutlineSprite;
    private RectTransform cautionNotice;
    private CanvasGroup cautionNoticeGroup;

    private void Awake()
    {
        EnsureAudioListener();
        Canvas canvas = GetComponentInParent<Canvas>();
        if (canvas != null)
        {
            canvas.pixelPerfect = true;
        }

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
            96
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
        }

        SetActive(warningPanel, false);
        SetActive(emergencyPanel, false);
        CreateAssistNotice();
        StyleCautionNotice();
        StyleEmergencyNotice();
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
        UpdateAssistNotice(state, emergencyActive);
        UpdateCautionNotice(state == "Caution" && !emergencyActive, emergencyActive || state == "Danger");
        if (previousDriverState == "Caution" && (emergencyActive || state != "Caution"))
        {
            if (audioSource != null)
            {
                audioSource.Stop();
            }
            nextWarningTime = 0f;
        }
        if (emergencyActive || state == "Danger")
        {
            previousDriverState = state;
            SetActive(emergencyPanel, true);
            return;
        }

        SetActive(emergencyPanel, false);
        if (state == "Caution")
        {
            if (warningText != null)
            {
                warningText.text = "운전자 상태를 확인하세요.";
            }
            if (previousDriverState != "Caution" || Time.unscaledTime >= nextWarningTime)
            {
                PlayWarningSound();
                nextWarningTime = Time.unscaledTime + WarningRepeatIntervalSeconds;
            }
        }
        previousDriverState = state;
    }

    private void StyleCautionNotice()
    {
        cautionNotice = StyleStateNotice(warningPanel, warningText, "Caution", "주의",
            new Color(1f, 0.64f, 0.28f), new Color(0.12f, 0.075f, 0.045f, 0.76f),
            new Vector2(680f, 140f));
        if (cautionNotice == null)
        {
            return;
        }
        cautionNoticeGroup = warningPanel.GetComponent<CanvasGroup>();
        if (cautionNoticeGroup == null)
        {
            cautionNoticeGroup = warningPanel.AddComponent<CanvasGroup>();
        }
        cautionNoticeGroup.alpha = 0f;
        cautionNoticeGroup.interactable = false;
        cautionNoticeGroup.blocksRaycasts = false;
    }

    private void StyleEmergencyNotice()
    {
        if (emergencyPanel == null)
        {
            return;
        }
        Transform messageObject = emergencyPanel.transform.Find("EmergencyMessage");
        Text message = messageObject != null ? messageObject.GetComponent<Text>()
            : emergencyPanel.GetComponentInChildren<Text>(true);
        StyleStateNotice(emergencyPanel, message, "Danger", "비상 정차",
            new Color(1f, 0.36f, 0.39f), new Color(0.10f, 0.035f, 0.045f, 0.76f),
            new Vector2(740f, 148f));
    }

    private RectTransform StyleStateNotice(GameObject panel, Text message, string prefix,
        string captionText, Color accent, Color background, Vector2 size)
    {
        if (panel == null || assistNoticeSprite == null)
        {
            return null;
        }
        RectTransform rect = panel.GetComponent<RectTransform>();
        if (rect == null)
        {
            return null;
        }
        rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 0.5f);
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.sizeDelta = size;
        rect.anchoredPosition = Vector2.zero;
        Image oldBackground = panel.GetComponent<Image>();
        if (oldBackground != null)
        {
            oldBackground.color = Color.clear;
            oldBackground.raycastTarget = false;
        }
        CreateNoticeImage(rect, prefix + "Shadow", size, new Vector2(0f, -4f),
            new Color(0f, 0f, 0f, 0.14f));
        CreateNoticeImage(rect, prefix + "Background", size - new Vector2(4f, 4f), Vector2.zero, background);
        CreateNoticeImage(rect, prefix + "FineEdge", size, Vector2.zero,
            new Color(accent.r, accent.g, accent.b, 0.8f), true);
        Vector2 iconPosition = new Vector2(-size.x * 0.5f + 62f, 0f);
        CreateNoticeImage(rect, prefix + "IconBackground", new Vector2(60f, 60f), iconPosition,
            new Color(accent.r, accent.g, accent.b, 0.16f));
        Text icon = CreateNoticeText(rect, prefix + "Icon", 38, iconPosition,
            new Vector2(60f, 60f), accent);
        icon.text = "!";
        icon.fontStyle = FontStyle.Bold;
        icon.alignment = TextAnchor.MiddleCenter;
        float contentWidth = size.x - 152f;
        Text caption = CreateNoticeText(rect, prefix + "Caption", 20, new Vector2(36f, 30f),
            new Vector2(contentWidth, 30f), accent);
        caption.text = captionText;
        caption.fontStyle = FontStyle.Bold;
        if (message != null)
        {
            ConfigureNoticeText(message, 34, new Vector2(contentWidth, 54f));
            message.rectTransform.anchorMin = message.rectTransform.anchorMax = new Vector2(0.5f, 0.5f);
            message.rectTransform.anchoredPosition = new Vector2(36f, -17f);
            message.fontStyle = FontStyle.Normal;
            message.alignment = TextAnchor.MiddleLeft;
            message.color = new Color(1f, 0.95f, 0.91f);
            message.transform.SetAsLastSibling();
        }
        return rect;
    }

    private void UpdateCautionNotice(bool visible, bool immediate)
    {
        if (cautionNoticeGroup == null)
        {
            SetActive(warningPanel, visible);
            return;
        }
        if (visible)
        {
            SetActive(warningPanel, true);
        }
        cautionNoticeGroup.alpha = immediate ? 0f
            : Mathf.MoveTowards(cautionNoticeGroup.alpha, visible ? 1f : 0f, Time.unscaledDeltaTime * 6f);
        float progress = Mathf.SmoothStep(0f, 1f, cautionNoticeGroup.alpha);
        cautionNotice.anchoredPosition = new Vector2(0f, 8f * (1f - progress));
        if (cautionNoticeGroup.alpha <= 0f)
        {
            SetActive(warningPanel, false);
        }
    }

    private void CreateAssistNotice()
    {
        if (GetComponentInParent<Canvas>() == null)
        {
            return;
        }

        assistNoticeSprite = CreateRoundedNoticeSprite(false, out assistNoticeTexture);
        noticeOutlineSprite = CreateRoundedNoticeSprite(true, out noticeOutlineTexture);

        GameObject notice = new GameObject("VehicleAssistNotice", typeof(RectTransform), typeof(CanvasGroup));
        notice.transform.SetParent(transform, false);
        assistNotice = notice.GetComponent<RectTransform>();
        assistNotice.anchorMin = assistNotice.anchorMax = new Vector2(0.5f, 0f);
        assistNotice.pivot = new Vector2(0.5f, 0.5f);
        assistNotice.sizeDelta = new Vector2(540f, 88f);
        assistNotice.anchoredPosition = new Vector2(0f, 90f);
        assistNoticeGroup = notice.GetComponent<CanvasGroup>();
        assistNoticeGroup.alpha = 0f;
        assistNoticeGroup.interactable = false;
        assistNoticeGroup.blocksRaycasts = false;

        CreateNoticeImage(assistNotice, "SoftShadow", new Vector2(540f, 88f), new Vector2(0f, -4f),
            new Color(0f, 0f, 0f, 0.16f));
        CreateNoticeImage(assistNotice, "SmokedBackground", new Vector2(536f, 84f), Vector2.zero,
            new Color(0.035f, 0.065f, 0.075f, 0.86f));
        CreateNoticeImage(assistNotice, "FineEdge", new Vector2(540f, 88f), Vector2.zero,
            new Color(0.35f, 0.78f, 0.69f, 0.44f), true);
        CreateNoticeImage(assistNotice, "StatusHalo", new Vector2(30f, 30f), new Vector2(-220f, 0f),
            new Color(0.30f, 0.88f, 0.74f, 0.14f));
        CreateNoticeImage(assistNotice, "StatusDot", new Vector2(9f, 9f), new Vector2(-220f, 0f),
            new Color(0.38f, 0.94f, 0.79f, 1f));
        Text caption = CreateNoticeText(assistNotice, "AssistCaption", 16, new Vector2(26f, 19f),
            new Vector2(440f, 24f), new Color(0.63f, 0.79f, 0.77f));
        caption.text = "주행 보조";
        assistNoticeText = CreateNoticeText(assistNotice, "AssistMessage", 28, new Vector2(26f, -12f),
            new Vector2(440f, 42f), new Color(0.94f, 0.98f, 0.97f));
        notice.SetActive(false);
    }

    private Sprite CreateRoundedNoticeSprite(bool outline, out Texture2D texture)
    {
        // Two texture pixels per UI unit. The outline has a transparent center,
        // so a bright border does not turn the translucent background opaque.
        const int size = 128;
        texture = new Texture2D(size, size, TextureFormat.RGBA32, false);
        texture.name = outline ? "NoticeRoundedOutline" : "NoticeRoundedFill";
        texture.hideFlags = HideFlags.DontSave;
        texture.filterMode = FilterMode.Bilinear;
        texture.wrapMode = TextureWrapMode.Clamp;
        Color[] pixels = new Color[size * size];
        Vector2 center = new Vector2(64f, 64f);
        for (int y = 0; y < size; y++)
        {
            for (int x = 0; x < size; x++)
            {
                float distance = Vector2.Distance(new Vector2(x + 0.5f, y + 0.5f), center);
                float alpha = Mathf.Clamp01(64f - distance);
                if (outline)
                {
                    alpha -= Mathf.Clamp01(60f - distance);
                }
                pixels[y * size + x] = new Color(1f, 1f, 1f, alpha);
            }
        }
        texture.SetPixels(pixels);
        texture.Apply(false, true);
        Sprite sprite = Sprite.Create(texture, new Rect(0f, 0f, size, size),
            new Vector2(0.5f, 0.5f), 200f, 0, SpriteMeshType.FullRect, new Vector4(63f, 63f, 63f, 63f));
        sprite.hideFlags = HideFlags.DontSave;
        return sprite;
    }

    private void CreateNoticeImage(Transform parent, string objectName, Vector2 size, Vector2 position, Color color,
        bool outline = false)
    {
        GameObject item = new GameObject(objectName, typeof(RectTransform), typeof(Image));
        item.transform.SetParent(parent, false);
        RectTransform rect = item.GetComponent<RectTransform>();
        rect.anchorMin = rect.anchorMax = new Vector2(0.5f, 0.5f);
        rect.sizeDelta = size;
        rect.anchoredPosition = position;
        Image image = item.GetComponent<Image>();
        image.sprite = outline ? noticeOutlineSprite : assistNoticeSprite;
        image.type = Image.Type.Sliced;
        image.fillCenter = !outline;
        image.color = color;
        image.raycastTarget = false;
    }

    private Text CreateNoticeText(Transform parent, string objectName, int fontSize, Vector2 position, Vector2 size, Color color)
    {
        GameObject item = new GameObject(objectName, typeof(RectTransform), typeof(Text));
        item.transform.SetParent(parent, false);
        Text text = item.GetComponent<Text>();
        ConfigureNoticeText(text, fontSize, size);
        text.alignment = TextAnchor.MiddleLeft;
        text.color = color;
        text.rectTransform.anchorMin = text.rectTransform.anchorMax = new Vector2(0.5f, 0.5f);
        text.rectTransform.anchoredPosition = position;
        return text;
    }

    private void ConfigureNoticeText(Text text, int fontSize, Vector2 size)
    {
        text.font = runtimeFont != null ? runtimeFont : Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        // Rasterize larger glyphs, then shrink the geometry to the intended UI
        // size instead of stretching a small font atlas on a high-DPI display.
        text.fontSize = fontSize * TextRenderScale;
        text.resizeTextForBestFit = false;
        text.alignByGeometry = true;
        text.raycastTarget = false;
        text.rectTransform.sizeDelta = size * TextRenderScale;
        text.rectTransform.localScale = new Vector3(1f / TextRenderScale, 1f / TextRenderScale, 1f);
    }

    private void UpdateAssistNotice(string state, bool emergencyActive)
    {
        if (assistNotice == null)
        {
            return;
        }

        VehicleControlPacket command = tcpBridge == null ? null : tcpBridge.LatestCommand;
        BasicWheelVehicleController vehicle = tcpBridge == null ? null : tcpBridge.vehicle;
        bool available = state == "Caution" && !emergencyActive && tcpBridge != null
            && tcpBridge.IsConnected && command != null && command.can_active
            && vehicle != null && vehicle.EffectiveControlMode == "Remote" && vehicle.IsRemoteCommandFresh;
        CautionLaneAssistController assist = vehicle == null ? null : vehicle.cautionLaneAssist;
        bool actualSteering = !emergencyActive && state != "Danger" && tcpBridge != null
            && tcpBridge.IsConnected && assist != null && assist.GeometryReady
            && assist.IsAuthorized && assist.IsActive;
        bool steering = actualSteering || (available && (assist == null || !assist.GeometryReady)
            && command.steer_correction_active);
        bool acceleration = available && command.accel_limit_active;
        bool visible = steering || acceleration;

        if (visible)
        {
            assistNoticeText.text = steering && acceleration ? "차선 복귀 · 가속 제한 중"
                : steering ? "차선 중앙으로 복귀 중" : "가속 제한 중";
            SetActive(assistNotice.gameObject, true);
        }

        // A short fade and eight-pixel rise; emergency UI always takes priority.
        assistNoticeGroup.alpha = emergencyActive || state == "Danger" ? 0f
            : Mathf.MoveTowards(assistNoticeGroup.alpha, visible ? 1f : 0f, Time.unscaledDeltaTime * 6f);
        float progress = Mathf.SmoothStep(0f, 1f, assistNoticeGroup.alpha);
        assistNotice.anchoredPosition = new Vector2(0f, 90f + 8f * progress);
        if (assistNoticeGroup.alpha <= 0f)
        {
            SetActive(assistNotice.gameObject, false);
        }
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
        if (noticeOutlineSprite != null)
        {
            Destroy(noticeOutlineSprite);
        }
        if (noticeOutlineTexture != null)
        {
            Destroy(noticeOutlineTexture);
        }
        if (assistNoticeSprite != null)
        {
            Destroy(assistNoticeSprite);
        }
        if (assistNoticeTexture != null)
        {
            Destroy(assistNoticeTexture);
        }
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
