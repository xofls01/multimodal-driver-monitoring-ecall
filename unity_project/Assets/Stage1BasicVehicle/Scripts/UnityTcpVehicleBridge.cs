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
}

[Serializable]
public class VehicleControlPacket
{
    public string type;
    public long sequence;
    public float throttle;
    public float brake;
    public float steering;
    public bool hazard;
    public bool ecall;
    public bool remote_enabled = true;
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
    [Range(1f, 60f)] public float stateSendRateHz = 20f;
    public bool enableRemoteModeOnCommand = true;

    public bool IsConnected { get; private set; }
    public string ConnectionStatus { get; private set; } = "Stopped";
    public VehicleControlPacket LatestCommand { get; private set; }

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
            control_mode = vehicle.controlMode.ToString(),
            hazard = vehicle.RemoteHazard,
            ecall = vehicle.RemoteEcall
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
            }
        }
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
