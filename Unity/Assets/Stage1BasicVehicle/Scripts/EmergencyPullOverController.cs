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
    public BoxCollider mainRoadSurface;
    public BoxCollider shoulderSurface;
    public BoxCollider vehicleChassisCollider;

    [Header("Simple Road Geometry")]
    [Tooltip("Stage 1 도로의 진행 방향(Y 회전각)")]
    public float roadHeadingDegrees = 0f;
    [Tooltip("차량 양쪽에 남길 최소 여유 거리")]
    public float sideClearanceMeters = 0.25f;
    [Tooltip("바퀴 중심에서 바깥쪽까지의 추가 폭")]
    public float wheelLateralAllowanceMeters = 0.12f;
    public float shoulderCenterToleranceMeters = 0.2f;
    [Tooltip("S자형 갓길 진입 경로의 최소 전진 거리")]
    public float laneChangeDistance = 55f;
    [Tooltip("고속일수록 진입 경로를 늘리는 속도 비례 계수")]
    public float laneChangeMetersPerKph = 0.95f;
    public float pathLookAheadDistanceMeters = 14f;
    [Tooltip("실제 횡속도로 앞선 위치를 예상하는 시간")]
    public float lateralLookAheadSeconds = 0.8f;

    [Header("Emergency Motion")]
    [Tooltip("비상등을 먼저 알린 뒤 감속과 조향을 함께 시작하는 시간")]
    public float hazardLeadTimeSeconds = 0.5f;
    [Tooltip("갓길 중앙에 도착할 때의 목표 속도")]
    public float shoulderArrivalSpeedKph = 28f;
    [Tooltip("차체 방향을 정렬하는 동안의 목표 속도")]
    public float straightenSpeedKph = 16f;
    public float speedBrakeKp = 0.012f;
    [Range(0f, 1f)] public float maximumLaneChangeBrake = 0.055f;
    [Range(0f, 1f)] public float finalStoppingBrake = 0.16f;
    [Range(0f, 1f)] public float pathSteeringInputLimit = 0.14f;
    public float headingErrorForFullInputDegrees = 20f;
    public float headingAlignmentToleranceDegrees = 2.5f;
    public float alignmentStableSeconds = 0.5f;
    public float lateralSpeedToleranceMetersPerSecond = 0.2f;
    public float stoppedSpeedKph = 0.8f;

    [Header("Input Smoothing")]
    [Tooltip("초당 비상 조향 입력 최대 변화량")]
    public float steeringSlewRatePerSecond = 0.12f;
    [Tooltip("초당 비상 제동 입력 최대 변화량")]
    public float brakeSlewRatePerSecond = 0.06f;
    [Tooltip("초당 비상 가속 입력 최대 변화량")]
    public float throttleSlewRatePerSecond = 0.3f;

    public EmergencyPhase Phase { get; private set; } = EmergencyPhase.Idle;
    public bool IsEmergencyActive => Phase != EmergencyPhase.Idle;
    public float ShoulderTargetX { get; private set; }

    private Rigidbody vehicleBody;
    private bool shoulderGeometryReady;
    private float phaseStartedAt;
    private float smoothedSteeringInput;
    private float smoothedBrakeInput;
    private float smoothedThrottleInput;
    private Vector3 laneChangeStartPosition;
    private float laneChangeStartSpeedKph;
    private float alignedSince = -1f;
    private bool finalBrakingStarted;

    private void Awake()
    {
        if (vehicle == null)
        {
            vehicle = GetComponent<BasicWheelVehicleController>();
        }
        vehicleBody = GetComponent<Rigidbody>();
        if (vehicleChassisCollider == null)
        {
            vehicleChassisCollider = GetComponent<BoxCollider>();
        }
    }

    private void FixedUpdate()
    {
        if (Phase == EmergencyPhase.Idle || vehicle == null)
        {
            return;
        }

        if (!shoulderGeometryReady)
        {
            UpdateWithoutShoulderGeometry();
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
                HoldStoppedPose();
                vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
                break;
        }
    }

    public void TriggerEmergency()
    {
        // Danger가 20Hz로 반복 수신되더라도 최초 한 번만 상태를 시작합니다.
        if (Phase != EmergencyPhase.Idle)
        {
            return;
        }

        ResetSmoothedInputs();
        shoulderGeometryReady = TryCalculateShoulderTarget();
        if (!shoulderGeometryReady)
        {
            Debug.LogError(
                "[Emergency] 도로·차량 크기로 안전한 갓길 정차 위치를 계산할 수 없습니다. 차선 변경 없이 정차합니다."
            );
        }
        laneChangeStartPosition = transform.position;
        laneChangeStartSpeedKph = Mathf.Max(
            vehicle.CurrentSpeedKph,
            shoulderArrivalSpeedKph
        );
        SetPhase(EmergencyPhase.SignalAndSlow);
        // hazard는 즉시 켜되 감속과 조향 입력은 FixedUpdate에서 서서히 올립니다.
        vehicle.SetEmergencyControl(0f, 0f, 0f, true, false);
        Debug.Log("[Emergency] Danger 감지: 갓길 정차를 시작합니다.");
    }

    public void ResetEmergency()
    {
        Phase = EmergencyPhase.Idle;
        phaseStartedAt = 0f;
        shoulderGeometryReady = false;
        ResetSmoothedInputs();
        if (vehicle != null)
        {
            vehicle.ClearEmergencyControl();
        }
    }

    private void UpdateSignalAndSlow()
    {
        // 먼저 비상등을 알리는 동안은 급제동하지 않고 직진 관성 주행합니다.
        ApplySmoothedEmergencyControl(0f, 0f, 0f, false);

        if (Time.realtimeSinceStartup - phaseStartedAt >= hazardLeadTimeSeconds)
        {
            laneChangeStartPosition = transform.position;
            laneChangeStartSpeedKph = Mathf.Max(
                vehicle.CurrentSpeedKph,
                shoulderArrivalSpeedKph
            );
            SetPhase(EmergencyPhase.MoveToShoulder);
        }
    }

    private void UpdateMoveToShoulder()
    {
        Vector3 roadForward = RoadForward();
        float forwardDistance = Mathf.Max(
            0f,
            Vector3.Dot(transform.position - laneChangeStartPosition, roadForward)
        );
        float speedScaledDistance =
            laneChangeStartSpeedKph * Mathf.Max(0f, laneChangeMetersPerKph);
        float safeLaneChangeDistance = Mathf.Max(
            1f,
            Mathf.Max(laneChangeDistance, speedScaledDistance)
        );
        float progress = Mathf.Clamp01(forwardDistance / safeLaneChangeDistance);
        float lookAheadProgress = Mathf.Clamp01(
            (forwardDistance + pathLookAheadDistanceMeters)
                / safeLaneChangeDistance
        );

        // SmoothStep 경로는 시작과 끝의 횡방향 기울기가 0이므로
        // 초반에는 천천히 조향하고 후반에는 자연스럽게 반대 조향합니다.
        float pathBlend = SmoothStep01(lookAheadProgress);
        float targetX = Mathf.Lerp(
            laneChangeStartPosition.x,
            ShoulderTargetX,
            pathBlend
        );
        Vector3 targetPoint =
            laneChangeStartPosition
            + roadForward
                * (forwardDistance + pathLookAheadDistanceMeters);
        targetPoint.x = targetX;
        targetPoint.y = transform.position.y;

        float desiredHeading = HeadingTo(
            targetPoint - PredictedLateralPosition()
        );
        float steering = SteeringToward(
            desiredHeading,
            pathSteeringInputLimit
        );

        float speedBlend = SmoothStep01(progress);
        float targetSpeedKph = Mathf.Lerp(
            laneChangeStartSpeedKph,
            shoulderArrivalSpeedKph,
            speedBlend
        );
        float brake = BrakeForTargetSpeed(
            targetSpeedKph,
            maximumLaneChangeBrake
        );

        ApplySmoothedEmergencyControl(0f, brake, steering, false);

        if (progress >= 1f)
        {
            alignedSince = -1f;
            SetPhase(EmergencyPhase.StraightenAndBrake);
        }
    }

    private void UpdateStraightenAndBrake()
    {
        float steering = ShoulderCenterSteering();

        float lateralError = Mathf.Abs(ShoulderTargetX - transform.position.x);
        float headingError = Mathf.Abs(
            Mathf.DeltaAngle(transform.eulerAngles.y, roadHeadingDegrees)
        );
        bool aligned = lateralError <= shoulderCenterToleranceMeters
            && headingError <= headingAlignmentToleranceDegrees
            && Mathf.Abs(vehicleBody.velocity.x)
                <= lateralSpeedToleranceMetersPerSecond;

        if (!finalBrakingStarted)
        {
            if (aligned)
            {
                if (alignedSince < 0f)
                {
                    alignedSince = Time.realtimeSinceStartup;
                }
                else if (
                    Time.realtimeSinceStartup - alignedSince
                    >= alignmentStableSeconds
                )
                {
                    finalBrakingStarted = true;
                }
            }
            else
            {
                alignedSince = -1f;
            }
        }

        if (finalBrakingStarted && vehicle.CurrentSpeedKph <= stoppedSpeedKph)
        {
            HoldStoppedPose();
            SetPhase(EmergencyPhase.Stopped);
            vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
            Debug.Log("[Emergency] 갓길 정차 완료: e-Call 상태를 활성화합니다.");
            return;
        }

        float brake = finalBrakingStarted
            ? finalStoppingBrake
            : BrakeForTargetSpeed(
                straightenSpeedKph,
                maximumLaneChangeBrake
            );
        ApplySmoothedEmergencyControl(0f, brake, steering, false);
    }

    private void ApplySmoothedEmergencyControl(
        float targetThrottle,
        float targetBrake,
        float targetSteering,
        bool ecall
    )
    {
        float deltaTime = Time.fixedDeltaTime;
        targetThrottle = Mathf.Clamp01(targetThrottle);
        targetBrake = Mathf.Clamp01(targetBrake);
        targetSteering = Mathf.Clamp(targetSteering, -1f, 1f);

        // 제동 요청 중에는 구동 토크가 겹치지 않도록 가속을 즉시 해제합니다.
        if (targetBrake > 0.001f)
        {
            smoothedThrottleInput = 0f;
        }

        smoothedBrakeInput = Mathf.MoveTowards(
            smoothedBrakeInput,
            targetBrake,
            Mathf.Max(0f, brakeSlewRatePerSecond) * deltaTime
        );

        float allowedThrottle = smoothedBrakeInput <= 0.001f
            ? targetThrottle
            : 0f;
        smoothedThrottleInput = Mathf.MoveTowards(
            smoothedThrottleInput,
            allowedThrottle,
            Mathf.Max(0f, throttleSlewRatePerSecond) * deltaTime
        );
        smoothedSteeringInput = Mathf.MoveTowards(
            smoothedSteeringInput,
            targetSteering,
            Mathf.Max(0f, steeringSlewRatePerSecond) * deltaTime
        );

        vehicle.SetEmergencyControl(
            smoothedThrottleInput,
            smoothedBrakeInput,
            smoothedSteeringInput,
            true,
            ecall
        );
    }

    private float ShoulderCenterSteering()
    {
        Vector3 targetPoint =
            transform.position
            + RoadForward() * pathLookAheadDistanceMeters;
        targetPoint.x = ShoulderTargetX;
        targetPoint.y = transform.position.y;
        float desiredHeading = HeadingTo(
            targetPoint - PredictedLateralPosition()
        );
        return SteeringToward(desiredHeading, pathSteeringInputLimit);
    }

    private float SteeringToward(
        float desiredHeadingDegrees,
        float steeringLimit
    )
    {
        float currentHeading = transform.eulerAngles.y;
        float headingError = Mathf.DeltaAngle(currentHeading, desiredHeadingDegrees);
        float normalized = headingError
            / Mathf.Max(1f, headingErrorForFullInputDegrees);
        float safeLimit = Mathf.Clamp01(steeringLimit);
        return Mathf.Clamp(normalized, -safeLimit, safeLimit);
    }

    private float BrakeForTargetSpeed(float targetSpeedKph, float maximumBrake)
    {
        float speedError = vehicle.CurrentSpeedKph - targetSpeedKph;
        return Mathf.Clamp(
            speedError * Mathf.Max(0f, speedBrakeKp),
            0f,
            Mathf.Clamp01(maximumBrake)
        );
    }

    private bool TryCalculateShoulderTarget()
    {
        ResolveRoadReferences();
        if (mainRoadSurface == null
            || shoulderSurface == null
            || vehicleChassisCollider == null)
        {
            return false;
        }

        float scaleX = Mathf.Abs(transform.lossyScale.x);
        float chassisLeft = Mathf.Max(
            0f,
            (vehicleChassisCollider.size.x * 0.5f
                - vehicleChassisCollider.center.x) * scaleX
        );
        float chassisRight = Mathf.Max(
            0f,
            (vehicleChassisCollider.size.x * 0.5f
                + vehicleChassisCollider.center.x) * scaleX
        );
        float vehicleLeft = chassisLeft;
        float vehicleRight = chassisRight;

        WheelCollider[] wheels =
        {
            vehicle.frontLeftCollider,
            vehicle.frontRightCollider,
            vehicle.rearLeftCollider,
            vehicle.rearRightCollider
        };
        foreach (WheelCollider wheel in wheels)
        {
            if (wheel == null)
            {
                continue;
            }

            float wheelX = transform.InverseTransformPoint(wheel.transform.position).x
                * scaleX;
            float wheelAllowance = Mathf.Max(0f, wheelLateralAllowanceMeters);
            vehicleLeft = Mathf.Max(vehicleLeft, -wheelX + wheelAllowance);
            vehicleRight = Mathf.Max(vehicleRight, wheelX + wheelAllowance);
        }

        float innerEdgeX = Mathf.Max(
            mainRoadSurface.bounds.max.x,
            shoulderSurface.bounds.min.x
        );
        float outerEdgeX = shoulderSurface.bounds.max.x;
        float clearance = Mathf.Max(0f, sideClearanceMeters);
        float safeCenterMinX = innerEdgeX + vehicleLeft + clearance;
        float safeCenterMaxX = outerEdgeX - vehicleRight - clearance;
        if (safeCenterMinX > safeCenterMaxX)
        {
            return false;
        }

        ShoulderTargetX = (safeCenterMinX + safeCenterMaxX) * 0.5f;
        Debug.Log(
            $"[Emergency] 갓길 {innerEdgeX:F2}~{outerEdgeX:F2}m, "
                + $"차량 좌우 폭 {vehicleLeft:F2}/{vehicleRight:F2}m, "
                + $"정차 목표 X={ShoulderTargetX:F2}m"
        );
        return true;
    }

    private void ResolveRoadReferences()
    {
        if (mainRoadSurface != null && shoulderSurface != null)
        {
            return;
        }

        EndlessRoadController road = FindObjectOfType<EndlessRoadController>();
        if (road == null || road.roadSegments == null)
        {
            return;
        }

        foreach (Transform segment in road.roadSegments)
        {
            if (segment == null)
            {
                continue;
            }

            Transform mainRoad = segment.Find("MainRoad");
            Transform shoulder = segment.Find("Shoulder");
            if (mainRoad != null && shoulder != null)
            {
                mainRoadSurface = mainRoad.GetComponent<BoxCollider>();
                shoulderSurface = shoulder.GetComponent<BoxCollider>();
                return;
            }
        }
    }

    private Vector3 PredictedLateralPosition()
    {
        Vector3 predictedPosition = transform.position;
        if (vehicleBody != null)
        {
            predictedPosition.x += vehicleBody.velocity.x
                * Mathf.Max(0f, lateralLookAheadSeconds);
        }
        return predictedPosition;
    }

    private void UpdateWithoutShoulderGeometry()
    {
        if (Phase == EmergencyPhase.Stopped
            || vehicle.CurrentSpeedKph <= stoppedSpeedKph)
        {
            SetPhase(EmergencyPhase.Stopped);
            vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
            return;
        }

        ApplySmoothedEmergencyControl(0f, 0.16f, 0f, false);
    }

    private void HoldStoppedPose()
    {
        if (vehicleBody == null)
        {
            return;
        }

        Vector3 position = vehicleBody.position;
        position.x = ShoulderTargetX;
        vehicleBody.position = position;

        Vector3 velocity = vehicleBody.velocity;
        vehicleBody.velocity = new Vector3(0f, velocity.y, 0f);
        vehicleBody.angularVelocity = Vector3.zero;
        vehicleBody.rotation = Quaternion.Euler(0f, roadHeadingDegrees, 0f);
    }

    private Vector3 RoadForward()
    {
        return Quaternion.Euler(0f, roadHeadingDegrees, 0f) * Vector3.forward;
    }

    private static float SmoothStep01(float value)
    {
        value = Mathf.Clamp01(value);
        return value * value * (3f - 2f * value);
    }

    private static float HeadingTo(Vector3 direction)
    {
        return Mathf.Atan2(direction.x, direction.z) * Mathf.Rad2Deg;
    }

    private void ResetSmoothedInputs()
    {
        smoothedSteeringInput = 0f;
        smoothedBrakeInput = 0f;
        smoothedThrottleInput = 0f;
        alignedSince = -1f;
        finalBrakingStarted = false;
    }

    private void SetPhase(EmergencyPhase nextPhase)
    {
        Phase = nextPhase;
        phaseStartedAt = Time.realtimeSinceStartup;
    }
}
