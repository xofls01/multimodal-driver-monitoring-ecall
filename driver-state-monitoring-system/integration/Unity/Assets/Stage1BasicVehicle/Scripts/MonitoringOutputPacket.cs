using System;

// JSON null numeric/bool values default to zero/false in JsonUtility.
// Always check the corresponding observed/calibrated/connected flag before using them.
[Serializable]
public class MonitoringOutputPacket
{
    public string schema, clip_id, session_id, processed_at_utc, state;
    public double source_time_seconds, prediction_age_seconds;
    public bool is_current;
    public MonitoringDangerEvent danger;
    public MonitoringVision vision;
    public MonitoringDriverResponse driver_response;
    public MonitoringQuality observation_quality;
    public MonitoringBiosignal biosignal;
    public MonitoringVehicleConfirmation vehicle_confirmation;
}
[Serializable]
public class MonitoringVehicleConfirmation
{
    public bool stop_confirmed, stop_request_observed, receipt_fresh, source_time_matches, clip_matches, session_matches;
    public string status, emergency_phase, confirmation_source;
    public double feedback_source_seconds, feedback_age_seconds, speed_kph;
}
[Serializable]
public class MonitoringDangerEvent
{
    public bool active, detected;
    public double source_seconds, first_source_seconds;
    public string detected_at_utc, event_id, recording_event_utc;
    public int event_count;
}
[Serializable]
public class MonitoringVision
{
    public string state;
    public double observation_source_seconds, observation_age_seconds, max_observation_age_seconds;
    public bool current, eyes_observed, eyes_closed, head_lowering_observed, head_lowered;
    public double eyes_closed_seconds, head_lowering_seconds, head_lowering_angle_degrees;
    public bool head_angle_calibrated, head_pitch_proxy_observed;
    public string head_angle_status, head_pitch_proxy_units;
    public double head_pitch_proxy, head_drop_ratio;
    public bool face_observed, face_visible, face_readable, hands_observed, hands_on_wheel;
    public int hands_on_wheel_count;
    public string hands_on_wheel_method;
    public bool physical_grip_confirmation_available, physical_grip_confirmed;
    public MonitoringDangerEvent danger;
}
[Serializable]
public class MonitoringVisualRecovery
{
    public double source_seconds;
    public string observed_at_utc, warning_request_id;
    public bool after_confirmed_warning, consciousness_confirmed;
}
[Serializable]
public class MonitoringExplicitResponse
{
    public string event_id, action, source, received_at_utc, warning_request_id;
    public double applied_source_seconds;
    public bool causes_stop_cancellation;
}
[Serializable]
public class MonitoringDriverResponse
{
    public bool warning_active, warning_display_confirmed, explicit_input_connected, cancel_attempted;
    public string warning_request_id;
    public double warning_started_source_seconds, warning_displayed_source_seconds;
    public MonitoringVisualRecovery visual_recovery;
    public MonitoringExplicitResponse last_confirmed_response;
}
[Serializable]
public class MonitoringCameraQuality
{
    public string input_kind;
    public bool frame_read_ok, hardware_health_available, camera_hardware_ok;
    public int image_width, image_height;
}
[Serializable]
public class MonitoringFaceQuality
{
    public bool face_detected;
    public bool pose_confidence_available;
    public float face_width_pixels, face_height_pixels, mean_luminance, contrast_std;
    public float interocular_distance_pixels, pose_person_detection_confidence;
}
[Serializable]
public class MonitoringSensorChannel
{
    public bool ready, contact, stale, reliable;
    public bool contact_known, signal_quality_available;
    public float signal_quality;
}
[Serializable]
public class MonitoringSensorChannels
{
    public MonitoringSensorChannel ecg, ppg;
}
[Serializable]
public class MonitoringSensorQuality
{
    public string input_kind;
    public bool sample_available;
    public double sample_timestamp, sample_age_seconds;
    public bool result_fresh, hardware_health_available, hardware_ok;
    public MonitoringSensorChannels channels;
}
[Serializable]
public class MonitoringQuality
{
    public bool judgment_confidence_calibrated, face_detection_confidence_available;
    public float judgment_confidence_score, face_detection_confidence;
    public string[] quality_reasons;
    public MonitoringFaceQuality face;
    public MonitoringCameraQuality camera;
    public MonitoringSensorQuality sensors;
}
[Serializable]
public class MonitoringBiosignal
{
    public double bpm, model_bpm;
    public string bpm_state, bpm_source, model_state;
    public bool bpm_verified_by_monitor, synthetic, model_bpm_available;
}
