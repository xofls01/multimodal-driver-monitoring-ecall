using UnityEngine;

[RequireComponent(typeof(BasicWheelVehicleController))]
public class HazardLightController : MonoBehaviour
{
    [Header("References")]
    public BasicWheelVehicleController vehicle;
    public Renderer[] lampRenderers;

    [Header("Blink Settings")]
    [Min(0.1f)] public float blinkIntervalSeconds = 0.45f;

    public bool IsBlinking { get; private set; }
    public bool LampsOn { get; private set; }

    private bool wasHazardRequested;
    private float nextToggleAt;

    private void Awake()
    {
        if (vehicle == null)
        {
            vehicle = GetComponent<BasicWheelVehicleController>();
        }
        SetLampState(false);
    }

    private void Update()
    {
        bool hazardRequested = vehicle != null && vehicle.RemoteHazard;
        IsBlinking = hazardRequested;

        if (!hazardRequested)
        {
            wasHazardRequested = false;
            SetLampState(false);
            return;
        }

        if (!wasHazardRequested)
        {
            // Danger 감지 또는 hazard 활성화 직후에는 즉시 켜진 상태로 시작합니다.
            wasHazardRequested = true;
            SetLampState(true);
            nextToggleAt = Time.unscaledTime + blinkIntervalSeconds;
            return;
        }

        if (Time.unscaledTime >= nextToggleAt)
        {
            SetLampState(!LampsOn);
            nextToggleAt = Time.unscaledTime + blinkIntervalSeconds;
        }
    }

    private void SetLampState(bool on)
    {
        LampsOn = on;
        if (lampRenderers == null)
        {
            return;
        }

        foreach (Renderer lampRenderer in lampRenderers)
        {
            if (lampRenderer != null)
            {
                lampRenderer.enabled = on;
            }
        }
    }

    private void OnDisable()
    {
        wasHazardRequested = false;
        IsBlinking = false;
        SetLampState(false);
    }
}
