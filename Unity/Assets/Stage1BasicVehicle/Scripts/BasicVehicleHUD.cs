using System.Globalization;
using UnityEngine;

public class BasicVehicleHUD : MonoBehaviour
{
    public BasicWheelVehicleController vehicle;
    public UnityTcpVehicleBridge tcpBridge;
    public CruiseControlController cruiseController;
    public HazardLightController hazardLightController;

    [Header("Speed Readout")]
    [Min(0.01f)] public float speedSmoothingSeconds = 0.16f;

    [Header("HUD Background")]
    [Range(0f, 1f)] public float panelOpacity = 0.65f;

    private const float TextRenderScale = 2f;
    private const float DigitWidth = 38f;
    private const float DigitHeight = 68f;
    private const float DigitSpacing = 10f;
    // Segments: top, upper right, lower right, bottom, lower left, upper left, middle.
    private static readonly int[] DigitMasks =
    {
        0x3f, 0x06, 0x5b, 0x4f, 0x66, 0x6d, 0x7d, 0x07, 0x7f, 0x6f
    };

    private static readonly Color ReadoutColor = new Color(0.96f, 0.98f, 1f);
    private static readonly Color CaptionColor = new Color(0.78f, 0.85f, 0.9f);
    private static readonly Color ConnectedColor = new Color(0.48f, 0.9f, 0.75f);
    private static readonly Color DisconnectedColor = new Color(0.83f, 0.85f, 0.88f);
    private static readonly Color ShadowColor = new Color(0.02f, 0.04f, 0.06f, 0.65f);

    private Font runtimeFont;
    private GUIStyle captionStyle;
    private GUIStyle positionStyle;
    private GUIStyle connectionStyle;
    private float displayedSpeed;
    private int lastSpeed = -1;
    private string speedText = "0";
    private string positionText = "X --  Y --  Z --";

    private void OnEnable()
    {
        displayedSpeed = vehicle == null ? 0f : Mathf.Max(0f, vehicle.CurrentSpeedKph);
        lastSpeed = -1;
    }

    private void Update()
    {
        if (vehicle == null)
        {
            speedText = "--";
            positionText = "X --  Y --  Z --";
            lastSpeed = -1;
            return;
        }

        // Smooth only the readout; vehicle physics and TCP values remain untouched.
        float targetSpeed = Mathf.Max(0f, vehicle.CurrentSpeedKph);
        float blend = 1f - Mathf.Exp(-Time.unscaledDeltaTime / Mathf.Max(0.01f, speedSmoothingSeconds));
        displayedSpeed = Mathf.Lerp(displayedSpeed, targetSpeed, blend);
        int speed = Mathf.RoundToInt(displayedSpeed);
        if (speed != lastSpeed)
        {
            speedText = speed.ToString(CultureInfo.InvariantCulture);
            lastSpeed = speed;
        }

        Vector3 position = vehicle.transform.position;
        positionText = string.Format(CultureInfo.InvariantCulture,
            "X {0:0.0}  Y {1:0.0}  Z {2:0.0}", position.x, position.y, position.z);
    }

    private void InitializeStyles()
    {
        if (captionStyle != null) return;

        runtimeFont = Font.CreateDynamicFontFromOSFont(
            new[] { "Menlo", "Consolas", "DejaVu Sans Mono", "Arial" }, 64);
        captionStyle = CreateTextStyle(12, TextAnchor.MiddleLeft, CaptionColor);
        positionStyle = CreateTextStyle(18, TextAnchor.MiddleRight, ReadoutColor);
        connectionStyle = CreateTextStyle(12, TextAnchor.MiddleRight, DisconnectedColor);
    }

    private GUIStyle CreateTextStyle(int size, TextAnchor alignment, Color color)
    {
        return new GUIStyle(GUI.skin.label)
        {
            font = runtimeFont != null ? runtimeFont : GUI.skin.font,
            fontSize = size * (int)TextRenderScale,
            alignment = alignment,
            padding = new RectOffset(),
            margin = new RectOffset(),
            wordWrap = false,
            normal = { textColor = color }
        };
    }

    private void OnGUI()
    {
        if (Event.current.type != EventType.Repaint) return;
        InitializeStyles();

        Matrix4x4 previousMatrix = GUI.matrix;
        Color previousColor = GUI.color;
        try
        {
            // Keep both corners visible in a small Game view as well as at full screen.
            float scale = Mathf.Max(0.01f, Mathf.Min(Screen.width / 1000f, Screen.height / 600f));
            GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1f));
            GUI.color = Color.white;
            float rightEdge = Screen.width / scale - 24f;

            GUI.color = new Color(0f, 0f, 0f, panelOpacity);
            Fill(new Rect(12f, 12f, 176f, 146f));
            Fill(new Rect(rightEdge - 326f, 12f, 342f, 106f));
            GUI.color = Color.white;

            DrawText(new Rect(24f, 20f, 160f, 22f), "SPEED", captionStyle);
            DrawSpeed(24f, 50f);
            DrawText(new Rect(24f, 126f, 140f, 22f), "km/h", captionStyle);

            captionStyle.alignment = TextAnchor.MiddleRight;
            DrawText(new Rect(rightEdge - 310f, 20f, 310f, 22f), "POSITION", captionStyle);
            captionStyle.alignment = TextAnchor.MiddleLeft;
            DrawText(new Rect(rightEdge - 310f, 48f, 310f, 28f), positionText, positionStyle);

            bool connected = tcpBridge != null && tcpBridge.IsConnected;
            connectionStyle.normal.textColor = connected ? ConnectedColor : DisconnectedColor;
            DrawText(new Rect(rightEdge - 310f, 82f, 310f, 22f),
                connected ? "\u25cf TCP CONNECTED" : "\u25cb TCP DISCONNECTED", connectionStyle);
        }
        finally
        {
            GUI.matrix = previousMatrix;
            GUI.color = previousColor;
        }
    }

    private void DrawSpeed(float x, float y)
    {
        // Right-align fixed-width digits so 9 -> 10 -> 100 never moves the units column.
        float start = x + Mathf.Max(0, 3 - speedText.Length) * (DigitWidth + DigitSpacing);
        for (int index = 0; index < speedText.Length; index++)
        {
            char character = speedText[index];
            int mask = character == '-' ? 0x40 : DigitMasks[character - '0'];
            float digitX = start + index * (DigitWidth + DigitSpacing);
            DrawDigit(digitX + 1.5f, y + 2f, mask, ShadowColor);
            DrawDigit(digitX, y, mask, ReadoutColor);
        }
    }

    private static void DrawDigit(float x, float y, int mask, Color color)
    {
        const float thickness = 5f;
        const float gap = 1.5f;
        float middle = (DigitHeight - thickness) * 0.5f;
        float horizontalWidth = DigitWidth - 2f * (thickness + gap);
        float verticalHeight = middle - thickness - 2f * gap;
        GUI.color = color;
        if ((mask & 0x01) != 0) Fill(new Rect(x + thickness + gap, y, horizontalWidth, thickness));
        if ((mask & 0x02) != 0) Fill(new Rect(x + DigitWidth - thickness, y + thickness + gap, thickness, verticalHeight));
        if ((mask & 0x04) != 0) Fill(new Rect(x + DigitWidth - thickness, y + middle + thickness + gap, thickness, verticalHeight));
        if ((mask & 0x08) != 0) Fill(new Rect(x + thickness + gap, y + DigitHeight - thickness, horizontalWidth, thickness));
        if ((mask & 0x10) != 0) Fill(new Rect(x, y + middle + thickness + gap, thickness, verticalHeight));
        if ((mask & 0x20) != 0) Fill(new Rect(x, y + thickness + gap, thickness, verticalHeight));
        if ((mask & 0x40) != 0) Fill(new Rect(x + thickness + gap, y + middle, horizontalWidth, thickness));
        GUI.color = Color.white;
    }

    private static void Fill(Rect rect)
    {
        GUI.DrawTexture(rect, Texture2D.whiteTexture);
    }

    private static void DrawText(Rect rect, string text, GUIStyle style)
    {
        Matrix4x4 previousMatrix = GUI.matrix;
        Color textColor = style.normal.textColor;
        try
        {
            // Render glyphs at twice their displayed size to keep small labels clear.
            GUI.matrix = previousMatrix * Matrix4x4.Scale(new Vector3(1f / TextRenderScale, 1f / TextRenderScale, 1f));
            Rect textRect = new Rect(rect.x * TextRenderScale, rect.y * TextRenderScale,
                rect.width * TextRenderScale, rect.height * TextRenderScale);
            style.normal.textColor = ShadowColor;
            GUI.Label(new Rect(textRect.x + 2f, textRect.y + 2f, textRect.width, textRect.height), text, style);
            style.normal.textColor = textColor;
            GUI.Label(textRect, text, style);
        }
        finally
        {
            style.normal.textColor = textColor;
            GUI.matrix = previousMatrix;
        }
    }

    private void OnDestroy()
    {
        if (runtimeFont != null) Destroy(runtimeFont);
    }
}
