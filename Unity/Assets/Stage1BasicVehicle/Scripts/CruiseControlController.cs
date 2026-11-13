using UnityEngine;

[RequireComponent(typeof(BasicWheelVehicleController))]
public class CruiseControlController : MonoBehaviour
{
    [Header("Cruise Control")]
    public bool cruiseEnabled = true;
    [Min(0f)] public float targetSpeedKph = 60f;
    [Min(0f)] public float speedDeadbandKph = 1.5f;

    [Header("P Controller Gains")]
    [Min(0f)] public float throttleKp = 0.08f;
    [Min(0f)] public float brakeKp = 0.10f;
    [Range(0f, 1f)] public float maximumThrottle = 0.75f;
    [Range(0f, 1f)] public float maximumBrake = 0.65f;

    public float LastThrottleRequest { get; private set; }
    public float LastBrakeRequest { get; private set; }

    public void CalculateControl(
        float currentSpeedKph,
        out float throttle,
        out float brake
    )
    {
        throttle = 0f;
        brake = 0f;

        if (!cruiseEnabled)
        {
            LastThrottleRequest = 0f;
            LastBrakeRequest = 0f;
            return;
        }

        float speedError = targetSpeedKph - currentSpeedKph;
        if (Mathf.Abs(speedError) <= speedDeadbandKph)
        {
            LastThrottleRequest = 0f;
            LastBrakeRequest = 0f;
            return;
        }

        if (speedError > 0f)
        {
            throttle = Mathf.Clamp(speedError * throttleKp, 0f, maximumThrottle);
        }
        else
        {
            brake = Mathf.Clamp(-speedError * brakeKp, 0f, maximumBrake);
        }

        LastThrottleRequest = throttle;
        LastBrakeRequest = brake;
    }

    public void SetEnabled(bool enabled)
    {
        cruiseEnabled = enabled;
        if (!enabled)
        {
            LastThrottleRequest = 0f;
            LastBrakeRequest = 0f;
        }
    }
}
