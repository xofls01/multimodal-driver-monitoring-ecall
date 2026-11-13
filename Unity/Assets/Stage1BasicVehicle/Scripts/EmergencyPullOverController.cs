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
        Stopped,
        StopInPlace
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
    [Header("Emergency Motion")]
    [Tooltip("감속하며 차체 방향을 안정화하는 최소 시간")]
    public float hazardLeadTimeSeconds = 0.5f;
    public float stabilizationSpeedKph = 42f;
    [Tooltip("갓길 중앙에 도착할 때의 목표 속도")]
    public float shoulderArrivalSpeedKph = 28f;
    [Tooltip("차체 방향을 정렬하는 동안의 목표 속도")]
    public float straightenSpeedKph = 16f;
    public float speedBrakeKp = 0.012f;
    [Range(0f, 1f)] public float maximumLaneChangeBrake = 0.055f;
    [Range(0f, 1f)] public float finalStoppingBrake = 0.16f;
    [Range(0f, 1f)] public float pathSteeringInputLimit = 0.14f;
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
    private float alignedSince = -1f;
    private bool finalBrakingStarted;
    private float vehicleHalfWidth;
    private float vehicleHalfLength;
    private float wheelbaseMeters = 2.7f;

    private const float MaxPlannedLateralSpeed = 0.9f;
    private const float MaxPlannedLateralAcceleration = 1.3f;
    private const float MaxRecoveryLateralAcceleration = 6f;
    private const float LateralSpeedGain = 0.7f;
    private const float LateralAccelerationGain = 1.3f;
    private const float HeadingGain = 0.5f;
    private const float LateralResponseDelaySeconds = 0.35f;
    private const float UrgentSteeringSlewRatePerSecond = 0.8f;
    private const float StabilizationHeadingToleranceDegrees = 8f;
    private const float StabilizationLateralSpeedTolerance = 0.7f;
    private const float MaximumStabilizationSeconds = 4f;
    private const float MaximumAlignmentSeconds = 8f;
    private const float MinimumApproachSpeedKph = 5f;
    private const float MaximumRecoverableHeadingDegrees = 70f;
    private const float RecoveryGroundMarginMeters = 8f;

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
        if (vehicleChassisCollider != null)
        {
            vehicleHalfLength = (
                Mathf.Abs(vehicleChassisCollider.center.z)
                + vehicleChassisCollider.size.z * 0.5f
            ) * Mathf.Abs(transform.lossyScale.z);
        }
        if (vehicle.frontLeftCollider != null && vehicle.rearLeftCollider != null)
        {
            wheelbaseMeters = Mathf.Abs(
                transform.InverseTransformPoint(vehicle.frontLeftCollider.transform.position).z
                - transform.InverseTransformPoint(vehicle.rearLeftCollider.transform.position).z
            ) * Mathf.Abs(transform.lossyScale.z);
        }
    }

    private void FixedUpdate()
    {
        if (Phase == EmergencyPhase.Idle || vehicle == null)
        {
            return;
        }

        if (Phase == EmergencyPhase.Stopped)
        {
            vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
            return;
        }
        if (Phase == EmergencyPhase.StopInPlace)
        {
            UpdateStopInPlace();
            return;
        }
        if (!shoulderGeometryReady || ShouldStopInPlace())
        {
            EnterStopInPlace("도로 경계 이탈 위험 또는 갓길 정보 없음");
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
        SetPhase(EmergencyPhase.SignalAndSlow);
        vehicle.SetEmergencyControl(0f, 0f, 0f, true, false);
        if (!shoulderGeometryReady || ShouldStopInPlace())
        {
            EnterStopInPlace("Danger 시작 위치 또는 진행 방향이 도로 경계에 위험함");
            return;
        }
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
        // 경로 진입 전에도 즉시 감속하고, 현재 횡속도와 방향을 안정화합니다.
        float steering = SteeringForTargetX(transform.position.x);
        float brake = BrakeForTargetSpeed(
            stabilizationSpeedKph,
            maximumLaneChangeBrake
        );
        ApplySmoothedEmergencyControl(0f, brake, steering, false);

        float elapsed = Time.realtimeSinceStartup - phaseStartedAt;
        float headingError = Mathf.Abs(
            Mathf.DeltaAngle(transform.eulerAngles.y, roadHeadingDegrees)
        );
        if (elapsed >= hazardLeadTimeSeconds
            && headingError <= StabilizationHeadingToleranceDegrees
            && Mathf.Abs(vehicleBody.linearVelocity.x)
                <= StabilizationLateralSpeedTolerance)
        {
            SetPhase(EmergencyPhase.MoveToShoulder);
        }
        else if (elapsed >= MaximumStabilizationSeconds
                 || vehicle.CurrentSpeedKph <= MinimumApproachSpeedKph)
        {
            EnterStopInPlace("진행 방향 안정화 실패");
        }
    }

    private void UpdateMoveToShoulder()
    {
        float steering = SteeringForTargetX(ShoulderTargetX);
        float brake = BrakeForTargetSpeed(
            shoulderArrivalSpeedKph,
            maximumLaneChangeBrake
        );
        ApplySmoothedEmergencyControl(0f, brake, steering, false);

        if (IsAlignedWithShoulder())
        {
            if (alignedSince < 0f)
            {
                alignedSince = Time.realtimeSinceStartup;
            }
            else if (Time.realtimeSinceStartup - alignedSince
                     >= alignmentStableSeconds)
            {
                alignedSince = -1f;
                SetPhase(EmergencyPhase.StraightenAndBrake);
            }
        }
        else
        {
            alignedSince = -1f;
        }

        if (!IsAlignedWithShoulder()
            && vehicle.CurrentSpeedKph <= MinimumApproachSpeedKph)
        {
            EnterStopInPlace("갓길 도착 전 차량 속도 부족");
        }
    }

    private void UpdateStraightenAndBrake()
    {
        float steering = SteeringForTargetX(ShoulderTargetX);
        bool aligned = IsAlignedWithShoulder();

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
            SetPhase(EmergencyPhase.Stopped);
            vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
            Debug.Log("[Emergency] 갓길 정차 완료: e-Call 상태를 활성화합니다.");
            return;
        }

        if (!finalBrakingStarted
            && Time.realtimeSinceStartup - phaseStartedAt >= MaximumAlignmentSeconds)
        {
            EnterStopInPlace("갓길 내 차체 정렬 실패");
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
            Mathf.Max(
                0f,
                IsOvershootingShoulderTarget()
                    ? UrgentSteeringSlewRatePerSecond
                    : steeringSlewRatePerSecond
            ) * deltaTime
        );

        vehicle.SetEmergencyControl(
            smoothedThrottleInput,
            smoothedBrakeInput,
            smoothedSteeringInput,
            true,
            ecall
        );
    }

    private bool IsAlignedWithShoulder()
    {
        return Mathf.Abs(ShoulderTargetX - transform.position.x)
                <= shoulderCenterToleranceMeters
            && Mathf.Abs(Mathf.DeltaAngle(
                transform.eulerAngles.y,
                roadHeadingDegrees
            )) <= headingAlignmentToleranceDegrees
            && Mathf.Abs(vehicleBody.linearVelocity.x)
                <= lateralSpeedToleranceMetersPerSecond;
    }

    private float SteeringForTargetX(float targetX)
    {
        float lateralSpeed = vehicleBody.linearVelocity.x;
        float forwardSpeed = Mathf.Max(
            2f,
            Vector3.Dot(vehicleBody.linearVelocity, RoadForward())
        );
        // 현재 위치만 따라가면 횡속도와 조향 응답 지연만큼 목표를 지나친 뒤에야 복귀합니다.
        float predictedX = PredictedLateralStopX(lateralSpeed);
        float desiredLateralSpeed = Mathf.Clamp(
            (targetX - predictedX) * LateralSpeedGain,
            -MaxPlannedLateralSpeed,
            MaxPlannedLateralSpeed
        );

        // 경계까지 남은 거리로 허용 횡속도를 줄여, 갓길 끝에서 늦게 반대 조향하지 않게 합니다.
        float halfWidth = ProjectedHalfWidth();
        float remainingSpace = desiredLateralSpeed >= 0f
            ? shoulderSurface.bounds.max.x - sideClearanceMeters
                - transform.position.x - halfWidth
            : transform.position.x - halfWidth
                - mainRoadSurface.bounds.min.x - sideClearanceMeters;
        float safeLateralSpeed = Mathf.Max(
            0f,
            Mathf.Sqrt(2f * MaxPlannedLateralAcceleration
                * Mathf.Max(0f, remainingSpace))
                - MaxPlannedLateralAcceleration * LateralResponseDelaySeconds
        );
        desiredLateralSpeed = Mathf.Clamp(
            desiredLateralSpeed,
            -safeLateralSpeed,
            safeLateralSpeed
        );

        float desiredHeading = Mathf.Atan2(
            desiredLateralSpeed,
            forwardSpeed
        ) * Mathf.Rad2Deg;
        float headingErrorRadians = Mathf.DeltaAngle(
            transform.eulerAngles.y,
            roadHeadingDegrees + desiredHeading
        ) * Mathf.Deg2Rad;
        float accelerationLimit = IsOvershootingShoulderTarget()
            ? MaxRecoveryLateralAcceleration
            : MaxPlannedLateralAcceleration;
        float lateralAcceleration = Mathf.Clamp(
            (desiredLateralSpeed - lateralSpeed) * LateralAccelerationGain
                + forwardSpeed * headingErrorRadians * HeadingGain,
            -accelerationLimit,
            accelerationLimit
        );
        float wheelAngleDegrees = Mathf.Atan(
            lateralAcceleration * wheelbaseMeters
                / Mathf.Max(4f, forwardSpeed * forwardSpeed)
        ) * Mathf.Rad2Deg;
        float speedRatio = Mathf.InverseLerp(
            0f,
            vehicle.maxSpeedKph,
            vehicle.CurrentSpeedKph
        );
        float steeringLimitFactor = Mathf.Lerp(1f, 0.45f, speedRatio);
        float normalizedSteering = wheelAngleDegrees / Mathf.Max(
            1f,
            vehicle.maxSteeringAngle * steeringLimitFactor
        );
        return Mathf.Clamp(
            normalizedSteering,
            -Mathf.Clamp01(pathSteeringInputLimit),
            Mathf.Clamp01(pathSteeringInputLimit)
        );
    }

    private float PredictedLateralStopX(float lateralSpeed)
    {
        return transform.position.x
            + lateralSpeed * LateralResponseDelaySeconds
            + Mathf.Sign(lateralSpeed) * lateralSpeed * lateralSpeed
                / (2f * MaxPlannedLateralAcceleration);
    }

    private bool IsOvershootingShoulderTarget()
    {
        if (!shoulderGeometryReady || vehicleBody == null)
        {
            return false;
        }

        float error = ShoulderTargetX - transform.position.x;
        float lateralSpeed = vehicleBody.linearVelocity.x;
        if (Phase == EmergencyPhase.SignalAndSlow)
        {
            return Mathf.Abs(lateralSpeed) > StabilizationLateralSpeedTolerance
                || Mathf.Abs(Mathf.DeltaAngle(
                    transform.eulerAngles.y,
                    roadHeadingDegrees
                )) > StabilizationHeadingToleranceDegrees;
        }
        return Mathf.Abs(lateralSpeed) > lateralSpeedToleranceMetersPerSecond
            && (Mathf.Abs(error) <= shoulderCenterToleranceMeters
                || Mathf.Sign(error) != Mathf.Sign(lateralSpeed)
                || Mathf.Sign(ShoulderTargetX - PredictedLateralStopX(lateralSpeed))
                    != Mathf.Sign(error));
    }

    private float ProjectedHalfWidth()
    {
        float yawRadians = Mathf.DeltaAngle(
            roadHeadingDegrees,
            transform.eulerAngles.y
        ) * Mathf.Deg2Rad;
        return vehicleHalfWidth * Mathf.Abs(Mathf.Cos(yawRadians))
            + vehicleHalfLength * Mathf.Abs(Mathf.Sin(yawRadians));
    }

    private bool ShouldStopInPlace()
    {
        if (vehicleBody == null || mainRoadSurface == null
            || shoulderSurface == null)
        {
            return true;
        }

        float headingError = Mathf.Abs(Mathf.DeltaAngle(
            roadHeadingDegrees,
            transform.eulerAngles.y
        ));
        if (vehicle.CurrentSpeedKph > 20f
            && headingError > MaximumRecoverableHeadingDegrees)
        {
            return true;
        }
        if (vehicle.CurrentSpeedKph > MinimumApproachSpeedKph
            && Vector3.Dot(vehicleBody.linearVelocity, RoadForward()) <= 0f)
        {
            return true;
        }

        // 잔디 지면 위에서는 반대 조향으로 복귀할 시간을 줍니다.
        // 예측 위치만 경계를 넘었다고 즉시 조향을 끊으면 그대로 미끄러집니다.
        float halfWidth = ProjectedHalfWidth();
        float safeMin = mainRoadSurface.bounds.min.x - RecoveryGroundMarginMeters;
        float safeMax = shoulderSurface.bounds.max.x + RecoveryGroundMarginMeters;
        return transform.position.x - halfWidth < safeMin
            || transform.position.x + halfWidth > safeMax;
    }

    private void EnterStopInPlace(string reason)
    {
        if (Phase == EmergencyPhase.StopInPlace
            || Phase == EmergencyPhase.Stopped)
        {
            return;
        }
        SetPhase(EmergencyPhase.StopInPlace);
        vehicle.SetEmergencyControl(
            0f,
            1f,
            shoulderGeometryReady ? SteeringForTargetX(ShoulderTargetX) : 0f,
            true,
            false
        );
        Debug.LogWarning($"[Emergency] 갓길 진입 중단: {reason}. 현재 위치에서 제동합니다.");
    }

    private void UpdateStopInPlace()
    {
        if (vehicle.CurrentSpeedKph <= stoppedSpeedKph)
        {
            SetPhase(EmergencyPhase.Stopped);
            vehicle.SetEmergencyControl(0f, 1f, 0f, true, true);
            Debug.Log("[Emergency] 현재 위치 정차 완료: e-Call 상태를 활성화합니다.");
            return;
        }
        vehicle.SetEmergencyControl(
            0f,
            1f,
            shoulderGeometryReady ? SteeringForTargetX(ShoulderTargetX) : 0f,
            true,
            false
        );
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
        vehicleHalfWidth = Mathf.Max(vehicleLeft, vehicleRight);

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

    private Vector3 RoadForward()
    {
        return Quaternion.Euler(0f, roadHeadingDegrees, 0f) * Vector3.forward;
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
