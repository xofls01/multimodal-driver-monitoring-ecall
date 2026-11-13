using UnityEngine;

#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
#endif

[RequireComponent(typeof(Rigidbody))]
public class BasicWheelVehicleController : MonoBehaviour
{
    public enum VehicleControlMode
    {
        Manual,
        Remote
    }

    [Header("Wheel Colliders")]
    public WheelCollider frontLeftCollider;
    public WheelCollider frontRightCollider;
    public WheelCollider rearLeftCollider;
    public WheelCollider rearRightCollider;

    [Header("Wheel Visual Pivots")]
    public Transform frontLeftVisual;
    public Transform frontRightVisual;
    public Transform rearLeftVisual;
    public Transform rearRightVisual;

    [Header("Vehicle Settings")]
    public float maxMotorTorque = 1800f;
    public float reverseMotorTorque = 900f;
    public float maxBrakeTorque = 3500f;
    public float handbrakeTorque = 5500f;
    public float maxSteeringAngle = 30f;
    public float maxSpeedKph = 120f;
    public float steeringResponse = 3.5f;
    public float rollingResistanceTorque = 25f;

    [Header("Control Mode")]
    public VehicleControlMode controlMode = VehicleControlMode.Manual;
    public CruiseControlController cruiseController;
    public CautionLaneAssistController cautionLaneAssist;
    public float remoteCommandTimeoutSeconds = 0.75f;
    [Range(0f, 1f)] public float connectionLostBrakeInput = 0.3f;

    public float CurrentSpeedKph { get; private set; }
    public float CurrentSteeringInput { get; private set; }
    public float CurrentThrottleInput { get; private set; }
    public float CurrentBrakeInput { get; private set; }
    public bool RemoteHazard { get; private set; }
    public bool RemoteEcall { get; private set; }
    public bool EmergencyOverrideActive { get; private set; }
    public string EffectiveControlMode
    {
        get
        {
            if (EmergencyOverrideActive)
            {
                return "Emergency";
            }
            if (controlMode == VehicleControlMode.Remote)
            {
                return "Remote";
            }
            if (cruiseController != null && cruiseController.cruiseEnabled)
            {
                return "Cruise";
            }
            return "Manual";
        }
    }
    public bool IsRemoteCommandFresh
    {
        get
        {
            return lastRemoteCommandTime >= 0f
                && Time.realtimeSinceStartup - lastRemoteCommandTime
                    <= remoteCommandTimeoutSeconds;
        }
    }

    private Rigidbody vehicleBody;
    private float throttleInput;
    private float steeringInput;
    private bool handbrakeInput;
    private float remoteThrottleInput;
    private float remoteBrakeInput;
    private float remoteSteeringInput;
    private float emergencyThrottleInput;
    private float emergencyBrakeInput;
    private float emergencySteeringInput;
    private float lastRemoteCommandTime = -1f;
    private Vector3 initialPosition;
    private Quaternion initialRotation;

    private void Awake()
    {
        vehicleBody = GetComponent<Rigidbody>();
        if (cruiseController == null)
        {
            cruiseController = GetComponent<CruiseControlController>();
        }
        initialPosition = transform.position;
        initialRotation = transform.rotation;

        // 무게중심을 낮춰 급조향 시 전복 가능성을 줄입니다.
        vehicleBody.centerOfMass = new Vector3(0f, -0.25f, 0f);
    }

    private void Update()
    {
        ReadKeyboardInput();

        if (ResetKeyPressed())
        {
            ResetVehicle();
        }

        if (ModeToggleKeyPressed())
        {
            controlMode = controlMode == VehicleControlMode.Manual
                ? VehicleControlMode.Remote
                : VehicleControlMode.Manual;
        }

        if (CruiseToggleKeyPressed() && cruiseController != null)
        {
            cruiseController.SetEnabled(!cruiseController.cruiseEnabled);
        }
    }

    private void FixedUpdate()
    {
#if UNITY_6000_0_OR_NEWER
        Vector3 currentVelocity = vehicleBody.linearVelocity;
#else
        Vector3 currentVelocity = vehicleBody.velocity;
#endif
        CurrentSpeedKph = currentVelocity.magnitude * 3.6f;
        float signedForwardSpeedKph =
            Vector3.Dot(transform.forward, currentVelocity) * 3.6f;

        bool useEmergencyControl = EmergencyOverrideActive;
        bool useRemoteControl = controlMode == VehicleControlMode.Remote;
        bool useCruiseControl = !useEmergencyControl
            && !useRemoteControl
            && cruiseController != null
            && cruiseController.cruiseEnabled;
        bool remoteCommandFresh = IsRemoteCommandFresh;
        float requestedSteering = useEmergencyControl
            ? emergencySteeringInput
            : useRemoteControl && remoteCommandFresh
                ? remoteSteeringInput
                : useRemoteControl || useCruiseControl ? 0f : steeringInput;

        if (cautionLaneAssist != null)
        {
            UnityTcpVehicleBridge bridge = cautionLaneAssist.bridge;
            bool laneAssistAuthorized = !useEmergencyControl && useRemoteControl
                && remoteCommandFresh && bridge != null
                && bridge.CurrentDriverState == "Caution"
                && bridge.LatestCommand != null && bridge.LatestCommand.can_active;
            requestedSteering = cautionLaneAssist.FilterSteering(
                requestedSteering, CurrentSpeedKph, Time.fixedDeltaTime, laneAssistAuthorized);
        }

        CurrentSteeringInput = Mathf.MoveTowards(
            CurrentSteeringInput,
            requestedSteering,
            steeringResponse * Time.fixedDeltaTime
        );

        float speedRatio = Mathf.InverseLerp(0f, maxSpeedKph, CurrentSpeedKph);
        float steeringLimitFactor = Mathf.Lerp(1f, 0.45f, speedRatio);
        float steeringAngle =
            CurrentSteeringInput * maxSteeringAngle * steeringLimitFactor;

        frontLeftCollider.steerAngle = steeringAngle;
        frontRightCollider.steerAngle = steeringAngle;

        float motorTorque = 0f;
        float brakeTorque = 0f;

        if (useEmergencyControl)
        {
            if (signedForwardSpeedKph < maxSpeedKph)
            {
                motorTorque = emergencyThrottleInput * maxMotorTorque;
            }
            brakeTorque = emergencyBrakeInput * maxBrakeTorque;
        }
        else if (useRemoteControl)
        {
            if (remoteCommandFresh)
            {
                if (signedForwardSpeedKph < maxSpeedKph)
                {
                    motorTorque = remoteThrottleInput * maxMotorTorque;
                }
                brakeTorque = remoteBrakeInput * maxBrakeTorque;
            }
            else
            {
                // 원격 명령이 끊기면 가속을 해제하고 완만하게 제동합니다.
                motorTorque = 0f;
                brakeTorque = connectionLostBrakeInput * maxBrakeTorque;
            }
        }
        else if (useCruiseControl)
        {
            cruiseController.CalculateControl(
                CurrentSpeedKph,
                out float cruiseThrottle,
                out float cruiseBrake
            );
            if (signedForwardSpeedKph < maxSpeedKph)
            {
                motorTorque = cruiseThrottle * maxMotorTorque;
            }
            brakeTorque = cruiseBrake * maxBrakeTorque;
        }
        else if (handbrakeInput)
        {
            brakeTorque = handbrakeTorque;
        }
        else if (throttleInput > 0f)
        {
            if (signedForwardSpeedKph < maxSpeedKph)
            {
                motorTorque = throttleInput * maxMotorTorque;
            }
        }
        else if (throttleInput < 0f)
        {
            // 전진 중 S를 누르면 브레이크, 거의 멈춘 뒤에는 후진합니다.
            if (signedForwardSpeedKph > 2f)
            {
                brakeTorque = -throttleInput * maxBrakeTorque;
            }
            else if (signedForwardSpeedKph > -25f)
            {
                motorTorque = throttleInput * reverseMotorTorque;
            }
        }
        else
        {
            brakeTorque = rollingResistanceTorque;
        }

        CurrentThrottleInput = Mathf.Clamp01(
            motorTorque == 0f ? 0f : Mathf.Abs(motorTorque) / maxMotorTorque
        );
        CurrentBrakeInput = Mathf.Clamp01(brakeTorque / maxBrakeTorque);

        // 기본 데모는 후륜 구동으로 구성합니다.
        rearLeftCollider.motorTorque = motorTorque;
        rearRightCollider.motorTorque = motorTorque;

        ApplyBrakeTorque(brakeTorque);
    }

    private void LateUpdate()
    {
        UpdateWheelVisual(frontLeftCollider, frontLeftVisual);
        UpdateWheelVisual(frontRightCollider, frontRightVisual);
        UpdateWheelVisual(rearLeftCollider, rearLeftVisual);
        UpdateWheelVisual(rearRightCollider, rearRightVisual);
    }

    private void ApplyBrakeTorque(float torque)
    {
        frontLeftCollider.brakeTorque = torque;
        frontRightCollider.brakeTorque = torque;
        rearLeftCollider.brakeTorque = torque;
        rearRightCollider.brakeTorque = torque;
    }

    private static void UpdateWheelVisual(
        WheelCollider wheelCollider,
        Transform visualPivot
    )
    {
        if (wheelCollider == null || visualPivot == null)
        {
            return;
        }

        wheelCollider.GetWorldPose(out Vector3 position, out Quaternion rotation);
        visualPivot.SetPositionAndRotation(position, rotation);
    }

    private void ReadKeyboardInput()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard keyboard = Keyboard.current;
        if (keyboard == null)
        {
            throttleInput = 0f;
            steeringInput = 0f;
            handbrakeInput = false;
            return;
        }

        bool forward = keyboard.wKey.isPressed || keyboard.upArrowKey.isPressed;
        bool backward = keyboard.sKey.isPressed || keyboard.downArrowKey.isPressed;
        bool left = keyboard.aKey.isPressed || keyboard.leftArrowKey.isPressed;
        bool right = keyboard.dKey.isPressed || keyboard.rightArrowKey.isPressed;

        throttleInput = (forward ? 1f : 0f) - (backward ? 1f : 0f);
        steeringInput = (right ? 1f : 0f) - (left ? 1f : 0f);
        handbrakeInput = keyboard.spaceKey.isPressed;
#else
        bool forward = Input.GetKey(KeyCode.W) || Input.GetKey(KeyCode.UpArrow);
        bool backward = Input.GetKey(KeyCode.S) || Input.GetKey(KeyCode.DownArrow);
        bool left = Input.GetKey(KeyCode.A) || Input.GetKey(KeyCode.LeftArrow);
        bool right = Input.GetKey(KeyCode.D) || Input.GetKey(KeyCode.RightArrow);

        throttleInput = (forward ? 1f : 0f) - (backward ? 1f : 0f);
        steeringInput = (right ? 1f : 0f) - (left ? 1f : 0f);
        handbrakeInput = Input.GetKey(KeyCode.Space);
#endif
    }

    private static bool ResetKeyPressed()
    {
#if ENABLE_INPUT_SYSTEM
        return Keyboard.current != null && Keyboard.current.rKey.wasPressedThisFrame;
#else
        return Input.GetKeyDown(KeyCode.R);
#endif
    }

    private static bool ModeToggleKeyPressed()
    {
#if ENABLE_INPUT_SYSTEM
        return Keyboard.current != null && Keyboard.current.mKey.wasPressedThisFrame;
#else
        return Input.GetKeyDown(KeyCode.M);
#endif
    }

    private static bool CruiseToggleKeyPressed()
    {
#if ENABLE_INPUT_SYSTEM
        return Keyboard.current != null && Keyboard.current.cKey.wasPressedThisFrame;
#else
        return Input.GetKeyDown(KeyCode.C);
#endif
    }

    public void SetRemoteControl(
        float throttle,
        float brake,
        float steering,
        bool hazard,
        bool ecall,
        bool switchToRemoteMode = true
    )
    {
        remoteThrottleInput = Mathf.Clamp01(throttle);
        remoteBrakeInput = Mathf.Clamp01(brake);
        remoteSteeringInput = Mathf.Clamp(steering, -1f, 1f);
        if (!EmergencyOverrideActive)
        {
            RemoteHazard = hazard;
            RemoteEcall = ecall;
        }
        lastRemoteCommandTime = Time.realtimeSinceStartup;

        if (switchToRemoteMode)
        {
            controlMode = VehicleControlMode.Remote;
        }
    }

    public void SetManualMode()
    {
        controlMode = VehicleControlMode.Manual;
        remoteThrottleInput = 0f;
        remoteBrakeInput = 0f;
        remoteSteeringInput = 0f;
        if (!EmergencyOverrideActive)
        {
            RemoteHazard = false;
            RemoteEcall = false;
        }
    }

    public void SetAuxiliaryState(bool hazard, bool ecall)
    {
        if (EmergencyOverrideActive)
        {
            return;
        }

        RemoteHazard = hazard;
        RemoteEcall = ecall;
    }

    public void SetEmergencyControl(
        float throttle,
        float brake,
        float steering,
        bool hazard,
        bool ecall
    )
    {
        EmergencyOverrideActive = true;
        emergencyThrottleInput = Mathf.Clamp01(throttle);
        emergencyBrakeInput = Mathf.Clamp01(brake);
        emergencySteeringInput = Mathf.Clamp(steering, -1f, 1f);
        RemoteHazard = hazard;
        RemoteEcall = ecall;
    }

    public void ClearEmergencyControl()
    {
        EmergencyOverrideActive = false;
        emergencyThrottleInput = 0f;
        emergencyBrakeInput = 0f;
        emergencySteeringInput = 0f;
        RemoteHazard = false;
        RemoteEcall = false;
    }

    public void ResetVehicle()
    {
        EmergencyPullOverController emergencyController =
            GetComponent<EmergencyPullOverController>();
        if (emergencyController != null)
        {
            emergencyController.ResetEmergency();
        }

        transform.SetPositionAndRotation(initialPosition, initialRotation);
#if UNITY_6000_0_OR_NEWER
        vehicleBody.linearVelocity = Vector3.zero;
#else
        vehicleBody.velocity = Vector3.zero;
#endif
        vehicleBody.angularVelocity = Vector3.zero;
        CurrentSteeringInput = 0f;
        throttleInput = 0f;
        steeringInput = 0f;
        handbrakeInput = false;
    }
}
