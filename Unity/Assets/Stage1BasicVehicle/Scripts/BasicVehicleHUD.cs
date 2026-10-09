using UnityEngine;

public class BasicVehicleHUD : MonoBehaviour
{
    public BasicWheelVehicleController vehicle;
    public UnityTcpVehicleBridge tcpBridge;

    private GUIStyle titleStyle;
    private GUIStyle valueStyle;
    private GUIStyle helpStyle;

    private void InitializeStyles()
    {
        if (titleStyle != null)
        {
            return;
        }

        titleStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 18,
            fontStyle = FontStyle.Bold,
            normal = { textColor = Color.white }
        };

        valueStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 16,
            normal = { textColor = Color.white }
        };

        helpStyle = new GUIStyle(GUI.skin.label)
        {
            fontSize = 13,
            normal = { textColor = new Color(0.9f, 0.9f, 0.9f) }
        };
    }

    private void OnGUI()
    {
        InitializeStyles();

        if (vehicle == null)
        {
            GUI.Box(new Rect(16f, 16f, 330f, 70f), string.Empty);
            GUI.Label(new Rect(30f, 28f, 300f, 30f), "Vehicle 연결 필요", titleStyle);
            return;
        }

        Vector3 position = vehicle.transform.position;

        GUI.Box(new Rect(16f, 16f, 420f, 228f), string.Empty);
        GUI.Label(new Rect(30f, 26f, 320f, 28f), "Stage 1 · Basic Vehicle", titleStyle);
        GUI.Label(
            new Rect(30f, 58f, 320f, 26f),
            $"속도  {vehicle.CurrentSpeedKph,6:F1} km/h",
            valueStyle
        );
        GUI.Label(
            new Rect(30f, 84f, 320f, 26f),
            $"위치  X {position.x:F1} / Z {position.z:F1}",
            valueStyle
        );
        GUI.Label(
            new Rect(30f, 110f, 320f, 26f),
            $"조향  {vehicle.CurrentSteeringInput,6:F2}",
            valueStyle
        );
        GUI.Label(
            new Rect(30f, 136f, 380f, 26f),
            $"제어  {vehicle.controlMode}",
            valueStyle
        );
        GUI.Label(
            new Rect(30f, 162f, 380f, 26f),
            tcpBridge == null
                ? "TCP   Bridge 없음"
                : $"TCP   {tcpBridge.ConnectionStatus}",
            valueStyle
        );
        GUI.Label(
            new Rect(30f, 194f, 380f, 30f),
            "W/S 가속·브레이크 · A/D 조향 · M 모드 · R 복귀",
            helpStyle
        );
    }
}
