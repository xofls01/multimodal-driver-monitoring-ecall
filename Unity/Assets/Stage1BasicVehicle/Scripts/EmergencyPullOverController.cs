using UnityEngine;

[RequireComponent(typeof(BasicWheelVehicleController))]
public class EmergencyPullOverController : MonoBehaviour
{
    public enum EmergencyPhase
    {
        Idle,
        SignalAndSlow,
        MoveToShoulder,
        StraightenAndBrake,
        Stopped
    }

    [Header("References")]
    public BasicWheelVehicleController vehicle;

    [Header("Simple Road Geometry")]
    [Tooltip("Stage 1 갓길 중앙의 월드 X 좌표")]
    public float shoulderCenterX = 4.5f;
    [Tooltip("Stage 1 도로의 진행 방향(Y 회전각)")]
    public float roadHeadingDegrees = 0f;
    public float shoulderArrivalTolerance = 0.3f;
    public float lookAheadDistance = 10f;

    [Header("Emergency Motion")]
    public float signalDelaySeconds = 0.8f;
    public float laneChangeSpeedKph = 20f;
    [Range(0f, 1f)] public float laneChangeThrottle = 0.25f;
    [Range(0f, 1f)] public float speedControlBrake = 0.2f;
    [Range(0f, 1f)] public float finalBrake = 0.65f;
    [Range(0f, 1f)] public float maximumSteeringInput = 0.35f;
    public float headingErrorForFullSteering = 25f;
    public float stoppedSpeedKph = 0.8f;

    public EmergencyPhase Phase { get; private set; } = EmergencyPhase.Idle;
    public bool IsEmergencyActive => Phase != EmergencyPhase.Idle;

    private float phaseStartedAt;

    private void Awake()
    {
        if (vehicle == null)
        {
            vehicle = GetComponent<BasicWheelVehicleController>();
        }
    }

    private void FixedUpdate()
    {
        if (Phase == EmergencyPhase.Idle || vehicle == null)
        {
            return;
        }

        switch (Phase)
        {
            case EmergencyPhase.SignalAndSlow:
                UpdateSignalAndSlow();
                break;
            case EmergencyPhase.MoveToShoulder:
                UpdateMoveToShoulder();
                break;
            case EmergencyPhase.StraightenAndBrake:
                UpdateStraightenAndBrake();
                break;
            case EmergencyPhase.Stopped:
                vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
                break;
        }
    }

    public void TriggerEmergency()
    {
        // E가 20Hz로 반복 수신되더라도 최초 한 번만 상태를 시작합니다.
        if (Phase != EmergencyPhase.Idle)
        {
            return;
        }

        SetPhase(EmergencyPhase.SignalAndSlow);
        vehicle.SetEmergencyControl(0f, speedControlBrake, 0f, true, false);
        Debug.Log("[Emergency] E 감지: 갓길 정차를 시작합니다.");
    }

    public void ResetEmergency()
    {
        Phase = EmergencyPhase.Idle;
        phaseStartedAt = 0f;
        if (vehicle != null)
        {
            vehicle.ClearEmergencyControl();
        }
    }

    private void UpdateSignalAndSlow()
    {
        float brake = vehicle.CurrentSpeedKph > laneChangeSpeedKph
            ? speedControlBrake
            : 0f;
        vehicle.SetEmergencyControl(0f, brake, 0f, true, false);

        if (Time.realtimeSinceStartup - phaseStartedAt >= signalDelaySeconds)
        {
            SetPhase(EmergencyPhase.MoveToShoulder);
        }
    }

    private void UpdateMoveToShoulder()
    {
        float lateralError = shoulderCenterX - transform.position.x;

        if (Mathf.Abs(lateralError) <= shoulderArrivalTolerance)
        {
            SetPhase(EmergencyPhase.StraightenAndBrake);
            return;
        }

        Vector3 targetPoint = new Vector3(
            shoulderCenterX,
            transform.position.y,
            transform.position.z + lookAheadDistance
        );
        float desiredHeading = HeadingTo(targetPoint - transform.position);
        float steering = SteeringToward(desiredHeading);

        float throttle = 0f;
        float brake = 0f;
        if (vehicle.CurrentSpeedKph < laneChangeSpeedKph - 1f)
        {
            throttle = laneChangeThrottle;
        }
        else if (vehicle.CurrentSpeedKph > laneChangeSpeedKph + 1f)
        {
            brake = speedControlBrake;
        }

        vehicle.SetEmergencyControl(throttle, brake, steering, true, false);
    }

    private void UpdateStraightenAndBrake()
    {
        float steering = SteeringToward(roadHeadingDegrees);

        if (vehicle.CurrentSpeedKph <= stoppedSpeedKph)
        {
            SetPhase(EmergencyPhase.Stopped);
            vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
            Debug.Log("[Emergency] 갓길 정차 완료: e-Call 상태를 활성화합니다.");
            return;
        }

        vehicle.SetEmergencyControl(0f, finalBrake, steering, true, false);
    }

    private float SteeringToward(float desiredHeadingDegrees)
    {
        float currentHeading = transform.eulerAngles.y;
        float headingError = Mathf.DeltaAngle(currentHeading, desiredHeadingDegrees);
        float normalized = headingError / Mathf.Max(1f, headingErrorForFullSteering);
        return Mathf.Clamp(normalized, -maximumSteeringInput, maximumSteeringInput);
    }

    private static float HeadingTo(Vector3 direction)
    {
        return Mathf.Atan2(direction.x, direction.z) * Mathf.Rad2Deg;
    }

    private void SetPhase(EmergencyPhase nextPhase)
    {
        Phase = nextPhase;
        phaseStartedAt = Time.realtimeSinceStartup;
    }
}
