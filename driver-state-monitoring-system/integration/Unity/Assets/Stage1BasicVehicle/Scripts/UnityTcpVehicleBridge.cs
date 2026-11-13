using System;
using System.Collections.Concurrent;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

[Serializable]
public class TcpVector3
{
    public float x;
    public float y;
    public float z;

    public TcpVector3(Vector3 value)
    {
        x = value.x;
        y = value.y;
        z = value.z;
    }
}

[Serializable]
public class VehicleStatePacket
{
    public string type = "vehicle_state";
    public long sequence;
    public double timestamp;
    public float speed_kph;
    public TcpVector3 position;
    public TcpVector3 forward;
    public float heading_deg;
    public float steering;
    public float throttle;
    public float brake;
    public string control_mode;
    public bool hazard;
    public bool ecall;
    public string driver_state;
    public string emergency_phase;
    public bool driver_response_input_connected;
    public string clip_id;
    public string session_id;
    public double source_timestamp;
    public bool has_source_timestamp;
}

[Serializable]
public class DriverObservationDurations
{
    public float eyes_closed;
    public float attention_away;
    public float attention_window_seconds;
    public float attention_unknown_seconds;
    public float mouth_open;
    public float after_displayed_warning;
}

[Serializable]
public class DriverFeatureReadiness
{
    public string feature;
    public string status;
}

[Serializable]
public class VehicleControlPacket
{
    public MonitoringOutputPacket monitoring;
    public string type;
    public long sequence;
    public float throttle;
    public float brake;
    public float steering;
    public bool hazard;
    public bool ecall;
    public bool remote_enabled = true;
    public string control_mode;
    public string driver_state;
    // Optional fields from the vision sender. Legacy control packets still work.
    public string vision_state;
    public bool vision_available = true;
    // Optional multimodal replay fields. Camera coverage remains separate from fusion coverage.
    public string pipeline;
    public string fusion_state;
    public bool fusion_available;
    public string bio_state;
    public bool bio_available;
    public bool synthetic_biosignal;
    public string clip_id;
    public string warning_request_id;
    public bool stop_requested;
    public bool full_system_ready;
    public long prediction_sequence;
    public string judgment_mode;
    public string[] reasons;
    public DriverObservationDurations durations;
    public DriverFeatureReadiness[] readiness;
    public bool warning_display_confirmed;
    public double warning_issued_at;
    public string warning_source;
}

[Serializable]
public class DriverWarningAcknowledgement
{
    public string type = "driver_warning_ack";
    public string warning_request_id;
    public string clip_id;
    public long prediction_sequence;
    public double unity_display_time;
}

[Serializable]
public class DriverResponseEvent
{
    public string type = "driver_response";
    public string clip_id;
    public string warning_request_id;
    public string event_id;
    public string action;
}

public class UnityTcpVehicleBridge : MonoBehaviour
{
    [Header("Python TCP Server")]
    public string host = "127.0.0.1";
    public int port = 5001;
    public float reconnectDelaySeconds = 1f;
    public float connectTimeoutSeconds = 2f;

    [Header("Vehicle State Transmission")]
    public BasicWheelVehicleController vehicle;
    public EmergencyPullOverController emergencyController;
    [Range(1f, 60f)] public float stateSendRateHz = 20f;
    public bool enableRemoteModeOnCommand = true;
    [Header("Vision Freshness")]
    public float predictionTimeoutSeconds = 1f;
    [Header("Explicit Driver Response (enable after wiring UI buttons)")]
    public bool driverResponseInputEnabled = false;

    public bool IsConnected { get; private set; }
    public string ConnectionStatus { get; private set; } = "Stopped";
    public VehicleControlPacket LatestCommand { get; private set; }
    public string CurrentDriverState { get; private set; } = "Unavailable";
    public bool StopRequestLatched { get; private set; }
    private float lastPredictionReceivedAt = -1f;

    private readonly ConcurrentQueue<string> incomingLines =
        new ConcurrentQueue<string>();
    private readonly ConcurrentQueue<string> outgoingLines =
        new ConcurrentQueue<string>();

    private Thread networkThread;
    private volatile bool running;
    private TcpClient activeClient;
    private float sendAccumulator;
    private long stateSequence;

    private void Start()
    {
        if (emergencyController == null && vehicle != null)
        {
            emergencyController = vehicle.GetComponent<EmergencyPullOverController>();
        }

        Application.runInBackground = true;
        running = true;
        networkThread = new Thread(ConnectionLoop)
        {
            IsBackground = true,
            Name = "Unity Vehicle TCP Bridge"
        };
        networkThread.Start();
    }

    private void Update()
    {
        ProcessIncomingCommands();
        if (!IsConnected || lastPredictionReceivedAt < 0f
            || Time.realtimeSinceStartup - lastPredictionReceivedAt > Mathf.Max(0.1f, predictionTimeoutSeconds))
        {
            // Lack of camera/network observations is not a Normal prediction.
            // An already started emergency remains latched in its controller.
            CurrentDriverState = "Unavailable";
        }

        if (vehicle == null || !IsConnected)
        {
            return;
        }

        sendAccumulator += Time.unscaledDeltaTime;
        float sendInterval = 1f / Mathf.Max(1f, stateSendRateHz);
        if (sendAccumulator >= sendInterval)
        {
            sendAccumulator %= sendInterval;
            outgoingLines.Enqueue(BuildVehicleStateJson());
        }
    }

    private string BuildVehicleStateJson()
    {
        VehicleStatePacket packet = new VehicleStatePacket
        {
            sequence = stateSequence++,
            timestamp = Time.realtimeSinceStartupAsDouble,
            speed_kph = vehicle.CurrentSpeedKph,
            position = new TcpVector3(vehicle.transform.position),
            forward = new TcpVector3(vehicle.transform.forward),
            heading_deg = vehicle.transform.eulerAngles.y,
            steering = vehicle.CurrentSteeringInput,
            throttle = vehicle.CurrentThrottleInput,
            brake = vehicle.CurrentBrakeInput,
            control_mode = vehicle.EffectiveControlMode,
            hazard = vehicle.RemoteHazard,
            ecall = vehicle.RemoteEcall,
            driver_state = CurrentDriverState,
            emergency_phase = emergencyController == null
                ? "Unavailable"
                : emergencyController.Phase.ToString(),
            driver_response_input_connected = driverResponseInputEnabled,
            clip_id = LatestCommand == null ? "" : LatestCommand.clip_id,
            session_id = LatestCommand == null || LatestCommand.monitoring == null
                ? "" : LatestCommand.monitoring.session_id,
            has_source_timestamp = LatestCommand != null && LatestCommand.monitoring != null
                && LatestCommand.monitoring.is_current,
            source_timestamp = LatestCommand == null || LatestCommand.monitoring == null
                ? 0.0 : LatestCommand.monitoring.source_time_seconds
        };

        return JsonUtility.ToJson(packet);
    }

    private void ProcessIncomingCommands()
    {
        int processed = 0;
        while (processed < 100 && incomingLines.TryDequeue(out string json))
        {
            processed++;
            VehicleControlPacket command;
            try
            {
                command = JsonUtility.FromJson<VehicleControlPacket>(json);
            }
            catch (Exception exception)
            {
                Debug.LogWarning($"Invalid TCP JSON: {exception.Message}\n{json}");
                continue;
            }

            if (command == null || command.type != "vehicle_control")
            {
                continue;
            }

            LatestCommand = command;

            if (!string.IsNullOrWhiteSpace(command.driver_state))
            {
                string driverState = command.driver_state.Trim();
                bool hasVisionAvailability = json.IndexOf("\"vision_available\"", StringComparison.Ordinal) >= 0;
                bool hasFusionAvailability = json.IndexOf("\"fusion_available\"", StringComparison.Ordinal) >= 0;
                bool observationUnavailable = hasFusionAvailability ? !command.fusion_available :
                    ((hasVisionAvailability && !command.vision_available)
                    || string.Equals(command.vision_state, "unavailable", StringComparison.OrdinalIgnoreCase));
                if (observationUnavailable
                    || string.Equals(driverState, "Unavailable", StringComparison.OrdinalIgnoreCase))
                {
                    CurrentDriverState = "Unavailable";
                    lastPredictionReceivedAt = Time.realtimeSinceStartup;
                }
                else if (string.Equals(driverState, "Normal", StringComparison.OrdinalIgnoreCase))
                {
                    CurrentDriverState = "Normal";
                    lastPredictionReceivedAt = Time.realtimeSinceStartup;
                }
                else if (string.Equals(driverState, "Caution", StringComparison.OrdinalIgnoreCase))
                {
                    CurrentDriverState = "Caution";
                    lastPredictionReceivedAt = Time.realtimeSinceStartup;
                }
                else if (string.Equals(driverState, "Danger", StringComparison.OrdinalIgnoreCase))
                {
                    CurrentDriverState = "Danger";
                    lastPredictionReceivedAt = Time.realtimeSinceStartup;
                }
            }

            if (CurrentDriverState == "Danger" && !StopRequestLatched)
            {
                StopRequestLatched = true;
                if (emergencyController != null)
                {
                    emergencyController.TriggerEmergency();
                }
                else
                {
                    Debug.LogError("Danger received, but EmergencyPullOverController is not assigned.");
                }
            }

            bool hasControlMode = json.IndexOf(
                "\"control_mode\"",
                StringComparison.Ordinal
            ) >= 0;
            bool hasLegacyRemoteEnabled = json.IndexOf(
                "\"remote_enabled\"",
                StringComparison.Ordinal
            ) >= 0;
            ApplyControlMode(command, hasControlMode, hasLegacyRemoteEnabled);
        }
    }

    private void ApplyControlMode(
        VehicleControlPacket command,
        bool hasControlMode,
        bool hasLegacyRemoteEnabled
    )
    {
        if (vehicle == null || StopRequestLatched
            || (emergencyController != null && emergencyController.IsEmergencyActive))
        {
            return;
        }

        CruiseControlController cruise = vehicle.cruiseController;
        string requestedMode = !hasControlMode
            || string.IsNullOrWhiteSpace(command.control_mode)
            ? string.Empty
            : command.control_mode.Trim().ToLowerInvariant();

        if (requestedMode == "remote")
        {
            vehicle.SetRemoteControl(
                command.throttle,
                command.brake,
                command.steering,
                command.hazard,
                command.ecall,
                true
            );
            return;
        }

        if (requestedMode == "cruise")
        {
            vehicle.SetManualMode();
            vehicle.SetAuxiliaryState(command.hazard, command.ecall);
            if (cruise != null)
            {
                cruise.SetEnabled(true);
            }
            return;
        }

        if (requestedMode == "manual")
        {
            vehicle.SetManualMode();
            vehicle.SetAuxiliaryState(command.hazard, command.ecall);
            if (cruise != null)
            {
                cruise.SetEnabled(false);
            }
            return;
        }

        if (hasControlMode)
        {
            Debug.LogWarning($"Unknown TCP control_mode: {command.control_mode}");
            return;
        }

        // 상태만 담긴 패킷은 현재 일반 제어 모드를 그대로 둡니다.
        if (!hasLegacyRemoteEnabled)
        {
            return;
        }

        // control_mode가 없는 기존 송신기는 remote_enabled 의미를 그대로 사용합니다.
        if (command.remote_enabled)
        {
            vehicle.SetRemoteControl(
                command.throttle,
                command.brake,
                command.steering,
                command.hazard,
                command.ecall,
                enableRemoteModeOnCommand
            );
        }
        else
        {
            vehicle.SetManualMode();
            if (cruise != null)
            {
                cruise.SetEnabled(false);
            }
        }
    }

    public void ReportDisplayedWarning()
    {
        VehicleControlPacket packet = LatestCommand;
        bool observationAvailable = packet != null &&
            (!string.IsNullOrEmpty(packet.pipeline) && packet.pipeline.StartsWith("multimodal", StringComparison.Ordinal)
                ? packet.fusion_available : packet.vision_available);
        if (!IsConnected || !observationAvailable
            || CurrentDriverState != "Caution" || string.IsNullOrEmpty(packet.warning_request_id))
        {
            return;
        }
        outgoingLines.Enqueue(JsonUtility.ToJson(new DriverWarningAcknowledgement
        {
            warning_request_id = packet.warning_request_id,
            clip_id = packet.clip_id,
            prediction_sequence = packet.prediction_sequence,
            unity_display_time = Time.realtimeSinceStartupAsDouble
        }));
    }

    // Bind these to real UI button presses. They report events; they do not cancel stopping.
    public void ReportDriverAcknowledgement() { ReportDriverResponse("acknowledge"); }
    public void ReportDriverCancelAttempt() { ReportDriverResponse("cancel_attempt"); }

    private void ReportDriverResponse(string action)
    {
        VehicleControlPacket packet = LatestCommand;
        if (!driverResponseInputEnabled || !IsConnected || packet == null
            || string.IsNullOrEmpty(packet.warning_request_id)) return;
        outgoingLines.Enqueue(JsonUtility.ToJson(new DriverResponseEvent
        {
            clip_id = packet.clip_id,
            warning_request_id = packet.warning_request_id,
            event_id = Guid.NewGuid().ToString("N"),
            action = action
        }));
    }

    // Call only AFTER the simulator's explicit emergency reset. Incoming vision
    // Normal/Unavailable packets never invoke this method or cancel stopping.
    public bool ResetStopRequestLatch()
    {
        if (vehicle == null || vehicle.CurrentSpeedKph > 0.1f
            || (emergencyController != null && emergencyController.IsEmergencyActive))
        {
            return false;
        }
        StopRequestLatched = false;
        return true;
    }

    private void ConnectionLoop()
    {
        UTF8Encoding utf8 = new UTF8Encoding(false);

        while (running)
        {
            TcpClient client = null;
            try
            {
                ConnectionStatus = $"Connecting {host}:{port}";
                client = new TcpClient { NoDelay = true };
                activeClient = client;

                IAsyncResult connectResult = client.BeginConnect(host, port, null, null);
                bool connected = connectResult.AsyncWaitHandle.WaitOne(
                    TimeSpan.FromSeconds(Math.Max(0.2f, connectTimeoutSeconds))
                );
                if (!connected)
                {
                    throw new TimeoutException("TCP connection timeout");
                }
                client.EndConnect(connectResult);

                NetworkStream stream = client.GetStream();
                IsConnected = true;
                ConnectionStatus = $"Connected {host}:{port}";

                byte[] receiveBuffer = new byte[8192];
                StringBuilder pendingText = new StringBuilder();

                while (running && client.Connected)
                {
                    while (outgoingLines.TryDequeue(out string outgoingJson))
                    {
                        byte[] bytes = utf8.GetBytes(outgoingJson + "\n");
                        stream.Write(bytes, 0, bytes.Length);
                    }

                    while (stream.DataAvailable)
                    {
                        int count = stream.Read(receiveBuffer, 0, receiveBuffer.Length);
                        if (count <= 0)
                        {
                            throw new SocketException();
                        }

                        pendingText.Append(utf8.GetString(receiveBuffer, 0, count));
                        ExtractCompleteLines(pendingText);
                    }

                    Thread.Sleep(5);
                }
            }
            catch (Exception exception)
            {
                if (running)
                {
                    ConnectionStatus = $"Disconnected: {exception.Message}";
                }
            }
            finally
            {
                IsConnected = false;
                try
                {
                    client?.Close();
                }
                catch
                {
                    // 종료 중 발생하는 소켓 오류는 무시합니다.
                }
                activeClient = null;

                // 연결이 끊긴 동안 오래된 상태 메시지가 쌓이지 않도록 비웁니다.
                while (outgoingLines.TryDequeue(out _))
                {
                }
            }

            if (running)
            {
                Thread.Sleep(Math.Max(100, (int)(reconnectDelaySeconds * 1000f)));
            }
        }
    }

    private void ExtractCompleteLines(StringBuilder pendingText)
    {
        while (true)
        {
            string text = pendingText.ToString();
            int newlineIndex = text.IndexOf('\n');
            if (newlineIndex < 0)
            {
                return;
            }

            string line = text.Substring(0, newlineIndex).TrimEnd('\r');
            pendingText.Remove(0, newlineIndex + 1);

            if (!string.IsNullOrWhiteSpace(line))
            {
                incomingLines.Enqueue(line);
            }
        }
    }

    private void OnDestroy()
    {
        StopNetworkThread();
    }

    private void OnApplicationQuit()
    {
        StopNetworkThread();
    }

    private void StopNetworkThread()
    {
        if (!running)
        {
            return;
        }

        running = false;
        try
        {
            activeClient?.Close();
        }
        catch
        {
            // 종료 중 발생하는 소켓 오류는 무시합니다.
        }

        if (networkThread != null && networkThread.IsAlive)
        {
            networkThread.Join(1500);
        }
    }
}
