using UnityEngine;

// Stage 1's known straight lane: no camera/lane classifier or path planner.
// Runs in the vehicle physics loop, independently of the 5 Hz raw CAN detector.
[RequireComponent(typeof(BasicWheelVehicleController))]
public class CautionLaneAssistController : MonoBehaviour
{
    [Header("References")]
    public BasicWheelVehicleController vehicle;
    public UnityTcpVehicleBridge bridge;

    [Header("Lane Safety Envelope")]
    public float roadHeadingDegrees;
    public float predictionSeconds = 1.1f;
    public float departureOffsetMeters = 0.45f;
    public float interventionClearanceMeters = 0.25f;
    public float responseDelaySeconds = 0.35f;
    public float wheelEdgeAllowanceMeters = 0.15f;

    [Header("Lateral Stabilization")]
    public float positionGain = 2.0f;
    public float lateralVelocityGain = 2.8f;
    public float maximumLateralAcceleration = 3.5f;
    public float steeringInputLimit = 0.14f;
    public float steeringSlewRate = 0.8f;
    public float releaseHoldSeconds = 1f;
    public float releaseRawAngleDegrees = 0.45f;
    public float releaseRawHoldSeconds = 0.1f;

    [Header("Delayed Caution Entry (Caution / Distraction)")]
    public float delayedEntryOffsetMeters = 0.15f;
    public float delayedEntryLateralSpeed = 0.25f;
    public float delayedEntryLookAheadSeconds = 0.15f;
    public float delayedEntryPositionGain = 1.3f;
    public float delayedEntrySteeringSlewRate = 1.2f;

    public bool IsActive { get; private set; }
    public bool IsAuthorized { get; private set; }
    public bool GeometryReady { get; private set; }
    public float LeftBoundary { get; private set; }
    public float RightBoundary { get; private set; }
    public float CurrentClearance { get; private set; }
    public float MinimumClearance { get; private set; }
    public float PredictedClearance { get; private set; }
    public float AppliedSteering { get; private set; }
    public string Reason { get; private set; } = "idle";
    public bool CanCompleteRecovery => IsActive && UsesIntermittentCautionScenario();

    private Rigidbody body;
    private float halfWidth;
    private float halfLength;
    private float wheelbase = 2.7f;
    private float neutralSince = -1f;
    private float poseStableSince = -1f;
    private bool previouslyAuthorized;
    private bool delayedEntryRecovery;
    private Vector3 roadRight;
    private Vector3 roadForward;

    private void Awake()
    {
        vehicle = GetComponent<BasicWheelVehicleController>();
        body = GetComponent<Rigidbody>();
    }

    private void Start()
    {
        ResolveGeometry();
    }

    private void ResolveGeometry()
    {
        if (vehicle == null || body == null) return;
        if (bridge != null && bridge.emergencyController != null)
            roadHeadingDegrees = bridge.emergencyController.roadHeadingDegrees;
        Quaternion roadRotation = Quaternion.Euler(0f, roadHeadingDegrees, 0f);
        roadRight = roadRotation * Vector3.right;
        roadForward = roadRotation * Vector3.forward;
        Renderer left = null;
        Renderer right = null;
        foreach (Renderer renderer in FindObjectsOfType<Renderer>())
        {
            if (renderer.name == "LaneDash" && left == null) left = renderer;
            if (renderer.name == "ShoulderBoundary" && right == null) right = renderer;
        }
        if (left == null || right == null) return;
        LeftBoundary = Vector3.Dot(left.bounds.center, roadRight)
            + ProjectExtent(left.bounds.extents, roadRight);
        RightBoundary = Vector3.Dot(right.bounds.center, roadRight)
            - ProjectExtent(right.bounds.extents, roadRight);
        BoxCollider chassis = vehicle.GetComponent<BoxCollider>();
        if (chassis == null) return;
        halfWidth = chassis.size.x * Mathf.Abs(vehicle.transform.lossyScale.x) * 0.5f;
        halfLength = chassis.size.z * Mathf.Abs(vehicle.transform.lossyScale.z) * 0.5f + 0.1f;
        WheelCollider[] wheels = vehicle.GetComponentsInChildren<WheelCollider>();
        foreach (WheelCollider wheel in wheels)
        {
            float lateral = Mathf.Abs(vehicle.transform.InverseTransformPoint(wheel.transform.position).x);
            halfWidth = Mathf.Max(halfWidth, lateral + wheelEdgeAllowanceMeters);
        }
        if (vehicle.frontLeftCollider != null && vehicle.rearLeftCollider != null)
            wheelbase = Vector3.Distance(vehicle.frontLeftCollider.transform.position,
                                        vehicle.rearLeftCollider.transform.position);
        GeometryReady = RightBoundary - LeftBoundary > 2f * halfWidth;
    }

    private static float ProjectExtent(Vector3 extent, Vector3 axis)
    {
        return Mathf.Abs(axis.x) * extent.x + Mathf.Abs(axis.y) * extent.y
            + Mathf.Abs(axis.z) * extent.z;
    }

    private bool UsesIntermittentCautionScenario()
    {
        VehicleControlPacket command = bridge == null ? null : bridge.LatestCommand;
        return command != null && (command.can_scenario == "caution" || command.can_scenario == "distraction");
    }

    private float Clearance(float lateralPosition, float heading)
    {
        // Include the front/rear corners when the chassis is at an angle.
        float extent = halfWidth * Mathf.Abs(Mathf.Cos(heading))
            + halfLength * Mathf.Abs(Mathf.Sin(heading));
        return Mathf.Min(lateralPosition - extent - LeftBoundary,
                         RightBoundary - lateralPosition - extent);
    }

    public float FilterSteering(float requested, float speedKph, float dt, bool authorized)
    {
        IsAuthorized = authorized;
        if (!GeometryReady) ResolveGeometry();
        if (!GeometryReady)
        {
            ResetAssist("lane_geometry_unavailable");
            return requested;
        }
        float lateral = Vector3.Dot(vehicle.transform.position, roadRight);
        float heading = Vector3.SignedAngle(roadForward, vehicle.transform.forward, Vector3.up)
            * Mathf.Deg2Rad;
#if UNITY_6000_0_OR_NEWER
        Vector3 velocity = body.linearVelocity;
#else
        Vector3 velocity = body.velocity;
#endif
        float lateralSpeed = Vector3.Dot(velocity, roadRight);
        float speed = Mathf.Max(0f, Vector3.Dot(velocity, vehicle.transform.forward));
        CurrentClearance = Clearance(lateral, heading);
        float speedFactor = Mathf.Lerp(1f, 0.45f,
            Mathf.InverseLerp(0f, vehicle.maxSpeedKph, speedKph));
        float predictedLateral = lateral;
        float predictedHeading = heading;
        float measuredYawRate = Vector3.Dot(body.angularVelocity, Vector3.up);
        float yaw = measuredYawRate;
        float targetYaw = speed * Mathf.Tan(requested * vehicle.maxSteeringAngle
            * speedFactor * Mathf.Deg2Rad) / Mathf.Max(0.1f, wheelbase);
        PredictedClearance = CurrentClearance;
        const int predictionSteps = 20;
        float step = predictionSeconds / predictionSteps;
        for (int index = 0; index < predictionSteps; index++)
        {
            yaw = Mathf.Lerp(yaw, targetYaw, Mathf.Clamp01(step / 0.25f));
            predictedHeading += yaw * step;
            float predictedSpeed = Mathf.Lerp(lateralSpeed, speed * Mathf.Sin(predictedHeading),
                Mathf.Clamp01((index + 1) * step / 0.25f));
            predictedLateral += predictedSpeed * step;
            PredictedClearance = Mathf.Min(PredictedClearance,
                Clearance(predictedLateral, predictedHeading));
        }
        if (!authorized)
        {
            previouslyAuthorized = false;
            ResetAssist("not_caution_can_control");
            AppliedSteering = requested;
            return requested;
        }
        bool cautionJustStarted = !previouslyAuthorized;
        MinimumClearance = previouslyAuthorized
            ? Mathf.Min(MinimumClearance, CurrentClearance) : CurrentClearance;
        previouslyAuthorized = true;
        float centerError = lateral - (LeftBoundary + RightBoundary) * 0.5f;
        bool movingOutward = centerError * lateralSpeed > 0.01f;
        float outwardSpeed = movingOutward ? Mathf.Abs(lateralSpeed) : 0f;
        float outwardAcceleration = movingOutward ? Mathf.Max(0f,
            Mathf.Sign(centerError) * speed * measuredYawRate * Mathf.Cos(heading)) : 0f;
        float speedAfterResponse = outwardSpeed + outwardAcceleration * responseDelaySeconds;
        float stoppingDistance = outwardSpeed * responseDelaySeconds
            + 0.5f * outwardAcceleration * responseDelaySeconds * responseDelaySeconds
            + speedAfterResponse * speedAfterResponse / (2f * maximumLateralAcceleration);
        bool actualDeparture = movingOutward && Mathf.Abs(centerError) >= departureOffsetMeters;
        bool recoveryMustStart = movingOutward
            && CurrentClearance <= stoppingDistance + interventionClearanceMeters;
        // A model warning can arrive after drift has already developed. Start
        // from the measured pose immediately, including an existing inward drift.
        bool delayedCaution = cautionJustStarted && UsesIntermittentCautionScenario()
            && (Mathf.Abs(centerError) >= delayedEntryOffsetMeters
                || Mathf.Abs(lateralSpeed) >= delayedEntryLateralSpeed
                || Mathf.Abs(heading) >= 1f * Mathf.Deg2Rad);
        // A hypothetical raw-CAN curve alone must not preempt all lateral motion.
        // Detect a measured departure, or intervene earlier if the remaining
        // vehicle-envelope clearance cannot absorb the outward stopping distance.
        if (!IsActive && (delayedCaution || actualDeparture || recoveryMustStart))
        {
            IsActive = true;
            delayedEntryRecovery = delayedCaution;
            neutralSince = -1f;
            AppliedSteering = vehicle.CurrentSteeringInput;
            Reason = delayedCaution ? "delayed_caution_existing_drift"
                : recoveryMustStart ? "outward_stopping_distance"
                : "measured_lateral_departure";
        }
        if (!IsActive)
        {
            AppliedSteering = requested;
            return requested;
        }
        float controlError = delayedEntryRecovery
            ? centerError + lateralSpeed * delayedEntryLookAheadSeconds : centerError;
        float controlGain = positionGain * (delayedEntryRecovery ? delayedEntryPositionGain : 1f);
        float desiredAcceleration = Mathf.Clamp(-controlGain * controlError
            - lateralVelocityGain * lateralSpeed,
            -maximumLateralAcceleration, maximumLateralAcceleration);
        float wheelAngle = Mathf.Atan(desiredAcceleration * wheelbase
            / Mathf.Max(9f, speed * speed)) * Mathf.Rad2Deg;
        float target = Mathf.Clamp(wheelAngle / (vehicle.maxSteeringAngle * speedFactor),
            -steeringInputLimit, steeringInputLimit);
        float slewRate = delayedEntryRecovery ? delayedEntrySteeringSlewRate : steeringSlewRate;
        AppliedSteering = Mathf.MoveTowards(AppliedSteering, target, slewRate * dt);
        // Track stable physical recovery separately from a near-neutral input.
        // Once recovered, hand control back at neutral instead of suppressing
        // the next independent drift while the raw detector is still latched.
        bool recovered = Mathf.Abs(centerError) <= 0.12f && Mathf.Abs(lateralSpeed) <= 0.12f
            && Mathf.Abs(heading) <= 0.5f * Mathf.Deg2Rad;
        if (recovered)
        {
            if (poseStableSince < 0f) poseStableSince = Time.fixedTime;
        }
        else poseStableSince = -1f;
        bool rawNearNeutral = Mathf.Abs(requested * vehicle.maxSteeringAngle)
            <= releaseRawAngleDegrees;
        if (rawNearNeutral)
        {
            if (neutralSince < 0f) neutralSince = Time.fixedTime;
        }
        else neutralSince = -1f;
        if (poseStableSince >= 0f && neutralSince >= 0f
            && Time.fixedTime - poseStableSince >= releaseHoldSeconds
            && Time.fixedTime - neutralSince >= releaseRawHoldSeconds)
        {
            ResetAssist("raw_neutral_and_pose_stable");
            AppliedSteering = requested;
        }
        return AppliedSteering;
    }

    private void ResetAssist(string reason)
    {
        IsActive = false;
        delayedEntryRecovery = false;
        neutralSince = -1f;
        poseStableSince = -1f;
        Reason = reason;
    }

    public void ResetForScenario()
    {
        previouslyAuthorized = false;
        AppliedSteering = vehicle == null ? 0f : vehicle.CurrentSteeringInput;
        ResetAssist("scenario_reset");
    }
}
