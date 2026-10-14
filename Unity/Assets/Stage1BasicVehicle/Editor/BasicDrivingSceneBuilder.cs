using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem.UI;
#endif

public static class BasicDrivingSceneBuilder
{
    private const string RootFolder = "Assets/Stage1BasicVehicle";
    private const string MaterialFolder = RootFolder + "/Materials";
    private const string SceneFolder = RootFolder + "/Scenes";

    [MenuItem("Tools/Emergency Driving Demo/Create Stage 1 Scene")]
    public static void CreateStage1Scene()
    {
        if (!Application.isBatchMode
            && !EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
        {
            return;
        }

        EnsureFolder(RootFolder);
        EnsureFolder(MaterialFolder);
        EnsureFolder(SceneFolder);

        Scene scene = EditorSceneManager.NewScene(
            NewSceneSetup.EmptyScene,
            NewSceneMode.Single
        );

        Material roadMaterial = GetOrCreateMaterial(
            MaterialFolder + "/Road.mat",
            new Color(0.16f, 0.17f, 0.18f)
        );
        Material shoulderMaterial = GetOrCreateMaterial(
            MaterialFolder + "/Shoulder.mat",
            new Color(0.25f, 0.26f, 0.25f)
        );
        Material lineMaterial = GetOrCreateMaterial(
            MaterialFolder + "/RoadLine.mat",
            new Color(0.95f, 0.94f, 0.82f)
        );
        Material bodyMaterial = GetOrCreateMaterial(
            MaterialFolder + "/VehicleBody.mat",
            new Color(0.74f, 0.48f, 0.24f)
        );
        Material windowMaterial = GetOrCreateMaterial(
            MaterialFolder + "/VehicleWindow.mat",
            new Color(0.12f, 0.17f, 0.18f)
        );
        Material wheelMaterial = GetOrCreateMaterial(
            MaterialFolder + "/Wheel.mat",
            new Color(0.035f, 0.035f, 0.035f)
        );

        EndlessRoadController roadController = CreateEnvironment(
            roadMaterial,
            shoulderMaterial,
            lineMaterial
        );
        BasicWheelVehicleController vehicle = CreateVehicle(
            bodyMaterial,
            windowMaterial,
            wheelMaterial
        );
        roadController.vehicle = vehicle.transform;
        UnityTcpVehicleBridge tcpBridge = CreateTcpBridge(vehicle);
        CreateCamera(vehicle.transform);
        CreateLighting();
        CreateDriverStateUI(tcpBridge);

        BasicVehicleHUD hud = new GameObject("VehicleHUD").AddComponent<BasicVehicleHUD>();
        hud.vehicle = vehicle;
        hud.tcpBridge = tcpBridge;
        hud.cruiseController = vehicle.cruiseController;

        string scenePath = SceneFolder + "/BasicDrivingStage1.unity";
        EditorSceneManager.SaveScene(scene, scenePath);
        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();

        Selection.activeGameObject = vehicle.gameObject;
        EditorGUIUtility.PingObject(vehicle.gameObject);

        if (!Application.isBatchMode)
        {
            EditorUtility.DisplayDialog(
                "Stage 1 Scene Created",
                "정속주행 차량, 재활용 도로 4개, TCP, 경고 UI를 생성했습니다.\n\nPlay하면 60km/h 정속주행을 시작합니다. C: Cruise, M: 모드, R: 초기화",
                "확인"
            );
        }
    }

    private static UnityTcpVehicleBridge CreateTcpBridge(
        BasicWheelVehicleController vehicle
    )
    {
        GameObject bridgeObject = new GameObject("PythonTcpBridge");
        UnityTcpVehicleBridge bridge = bridgeObject.AddComponent<UnityTcpVehicleBridge>();
        bridge.vehicle = vehicle;
        bridge.emergencyController = vehicle.GetComponent<EmergencyPullOverController>();
        bridge.host = "127.0.0.1";
        bridge.port = 5001;
        bridge.stateSendRateHz = 20f;
        return bridge;
    }

    private static EndlessRoadController CreateEnvironment(
        Material roadMaterial,
        Material shoulderMaterial,
        Material lineMaterial
    )
    {
        GameObject environment = new GameObject("Environment");
        const int segmentCount = 4;
        const float segmentLength = 100f;
        Transform[] segments = new Transform[segmentCount];

        for (int index = 0; index < segmentCount; index++)
        {
            GameObject segment = new GameObject($"RoadSegment_{index}");
            segment.transform.SetParent(environment.transform, false);
            segment.transform.position = new Vector3(
                0f,
                0f,
                -50f + index * segmentLength
            );
            segments[index] = segment.transform;

            // 일반 도로 x=-6~3, 갓길 x=3~6. 모든 좌표는 세그먼트 로컬 좌표입니다.
            CreateCube(
                "MainRoad",
                new Vector3(-1.5f, -0.1f, 0f),
                new Vector3(9f, 0.2f, segmentLength),
                roadMaterial,
                segment.transform,
                true,
                true
            );
            CreateCube(
                "Shoulder",
                new Vector3(4.5f, -0.1f, 0f),
                new Vector3(3f, 0.2f, segmentLength),
                shoulderMaterial,
                segment.transform,
                true,
                true
            );

            CreateSegmentLine("LeftEdge", -5.9f, segmentLength, lineMaterial, segment.transform);
            CreateSegmentLine("ShoulderBoundary", 3f, segmentLength, lineMaterial, segment.transform);
            CreateSegmentLine("RightEdge", 5.9f, segmentLength, lineMaterial, segment.transform);

            for (float localZ = -45f; localZ <= 45f; localZ += 10f)
            {
                CreateCube(
                    "LaneDash",
                    new Vector3(-1.5f, 0.012f, localZ),
                    new Vector3(0.12f, 0.025f, 5f),
                    lineMaterial,
                    segment.transform,
                    false,
                    true
                );
            }
        }

        EndlessRoadController controller =
            environment.AddComponent<EndlessRoadController>();
        controller.roadSegments = segments;
        controller.segmentLength = segmentLength;
        controller.recycleBehindDistance = 20f;
        return controller;
    }

    private static BasicWheelVehicleController CreateVehicle(
        Material bodyMaterial,
        Material windowMaterial,
        Material wheelMaterial
    )
    {
        GameObject vehicle = new GameObject("PlayerVehicle");
        vehicle.transform.position = new Vector3(0.75f, 0.45f, -5f);

        Rigidbody rigidbody = vehicle.AddComponent<Rigidbody>();
        rigidbody.mass = 1400f;
        rigidbody.drag = 0.02f;
        rigidbody.angularDrag = 0.6f;
        rigidbody.interpolation = RigidbodyInterpolation.Interpolate;
        rigidbody.collisionDetectionMode = CollisionDetectionMode.ContinuousDynamic;

        BoxCollider chassisCollider = vehicle.AddComponent<BoxCollider>();
        chassisCollider.center = new Vector3(0f, 0.28f, 0f);
        chassisCollider.size = new Vector3(1.8f, 0.55f, 4f);

        GameObject body = CreateCube(
            "Body",
            Vector3.zero,
            new Vector3(1.8f, 0.55f, 4f),
            bodyMaterial,
            vehicle.transform,
            false,
            true
        );
        body.transform.localPosition = new Vector3(0f, 0.28f, 0f);

        GameObject cabin = CreateCube(
            "Cabin",
            Vector3.zero,
            new Vector3(1.5f, 0.65f, 1.9f),
            windowMaterial,
            vehicle.transform,
            false,
            true
        );
        cabin.transform.localPosition = new Vector3(0f, 0.85f, -0.15f);

        WheelCollider frontLeft = CreateWheelCollider(vehicle.transform, "WheelCollider_FL", new Vector3(-0.85f, -0.05f, 1.35f));
        WheelCollider frontRight = CreateWheelCollider(vehicle.transform, "WheelCollider_FR", new Vector3(0.85f, -0.05f, 1.35f));
        WheelCollider rearLeft = CreateWheelCollider(vehicle.transform, "WheelCollider_RL", new Vector3(-0.85f, -0.05f, -1.35f));
        WheelCollider rearRight = CreateWheelCollider(vehicle.transform, "WheelCollider_RR", new Vector3(0.85f, -0.05f, -1.35f));

        Transform frontLeftVisual = CreateWheelVisual(vehicle.transform, "WheelVisual_FL", new Vector3(-0.85f, -0.05f, 1.35f), wheelMaterial);
        Transform frontRightVisual = CreateWheelVisual(vehicle.transform, "WheelVisual_FR", new Vector3(0.85f, -0.05f, 1.35f), wheelMaterial);
        Transform rearLeftVisual = CreateWheelVisual(vehicle.transform, "WheelVisual_RL", new Vector3(-0.85f, -0.05f, -1.35f), wheelMaterial);
        Transform rearRightVisual = CreateWheelVisual(vehicle.transform, "WheelVisual_RR", new Vector3(0.85f, -0.05f, -1.35f), wheelMaterial);

        BasicWheelVehicleController controller = vehicle.AddComponent<BasicWheelVehicleController>();
        controller.frontLeftCollider = frontLeft;
        controller.frontRightCollider = frontRight;
        controller.rearLeftCollider = rearLeft;
        controller.rearRightCollider = rearRight;
        controller.frontLeftVisual = frontLeftVisual;
        controller.frontRightVisual = frontRightVisual;
        controller.rearLeftVisual = rearLeftVisual;
        controller.rearRightVisual = rearRightVisual;

        CruiseControlController cruise =
            vehicle.AddComponent<CruiseControlController>();
        cruise.cruiseEnabled = true;
        cruise.targetSpeedKph = 60f;
        cruise.speedDeadbandKph = 1.5f;
        controller.cruiseController = cruise;

        EmergencyPullOverController emergency =
            vehicle.AddComponent<EmergencyPullOverController>();
        emergency.vehicle = controller;
        emergency.shoulderCenterX = 4.5f;
        emergency.roadHeadingDegrees = 0f;

        return controller;
    }

    private static void CreateDriverStateUI(UnityTcpVehicleBridge tcpBridge)
    {
        GameObject canvasObject = new GameObject("DriverStateCanvas");
        Canvas canvas = canvasObject.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.ScreenSpaceOverlay;
        canvas.sortingOrder = 20;

        CanvasScaler scaler = canvasObject.AddComponent<CanvasScaler>();
        scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
        scaler.referenceResolution = new Vector2(1920f, 1080f);
        scaler.matchWidthOrHeight = 0.5f;
        canvasObject.AddComponent<GraphicRaycaster>();

        Font font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");

        GameObject warningPanel = CreateUiPanel(
            "DriverWarningPanel",
            canvasObject.transform,
            new Color(0.12f, 0.12f, 0.12f, 0.96f),
            new Vector2(620f, 250f),
            new Vector2(0f, 30f)
        );
        Text warningText = CreateUiText(
            "WarningMessage",
            warningPanel.transform,
            font,
            "운전자 상태 경고",
            34,
            TextAnchor.MiddleCenter,
            new Vector2(560f, 100f),
            new Vector2(0f, 45f)
        );
        warningText.color = new Color(1f, 0.84f, 0.25f);

        GameObject buttonObject = CreateUiPanel(
            "AcknowledgeButton",
            warningPanel.transform,
            new Color(0.92f, 0.58f, 0.08f, 1f),
            new Vector2(220f, 72f),
            new Vector2(0f, -65f)
        );
        Button acknowledgeButton = buttonObject.AddComponent<Button>();
        acknowledgeButton.targetGraphic = buttonObject.GetComponent<Image>();
        Text buttonText = CreateUiText(
            "Label",
            buttonObject.transform,
            font,
            "주의",
            30,
            TextAnchor.MiddleCenter,
            new Vector2(220f, 72f),
            Vector2.zero
        );
        buttonText.color = Color.black;

        GameObject emergencyPanel = CreateUiPanel(
            "EmergencyPanel",
            canvasObject.transform,
            new Color(0.62f, 0.04f, 0.04f, 0.96f),
            new Vector2(720f, 110f),
            new Vector2(0f, 420f)
        );
        Text emergencyText = CreateUiText(
            "EmergencyMessage",
            emergencyPanel.transform,
            font,
            "비상 상태 감지 · 갓길 정차 중",
            36,
            TextAnchor.MiddleCenter,
            new Vector2(680f, 90f),
            Vector2.zero
        );
        emergencyText.color = Color.white;

        DriverStateWarningUI warningUI =
            canvasObject.AddComponent<DriverStateWarningUI>();
        warningUI.tcpBridge = tcpBridge;
        warningUI.warningPanel = warningPanel;
        warningUI.warningText = warningText;
        warningUI.acknowledgeButton = acknowledgeButton;
        warningUI.emergencyPanel = emergencyPanel;
        warningUI.emergencyController = tcpBridge.emergencyController;

        warningPanel.SetActive(false);
        emergencyPanel.SetActive(false);

        GameObject eventSystemObject = new GameObject("EventSystem");
        eventSystemObject.AddComponent<EventSystem>();
#if ENABLE_INPUT_SYSTEM
        eventSystemObject.AddComponent<InputSystemUIInputModule>();
#else
        eventSystemObject.AddComponent<StandaloneInputModule>();
#endif
    }

    private static GameObject CreateUiPanel(
        string name,
        Transform parent,
        Color color,
        Vector2 size,
        Vector2 anchoredPosition
    )
    {
        GameObject panel = new GameObject(name, typeof(RectTransform));
        panel.transform.SetParent(parent, false);
        RectTransform rect = panel.GetComponent<RectTransform>();
        rect.anchorMin = new Vector2(0.5f, 0.5f);
        rect.anchorMax = new Vector2(0.5f, 0.5f);
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.sizeDelta = size;
        rect.anchoredPosition = anchoredPosition;
        Image image = panel.AddComponent<Image>();
        image.color = color;
        return panel;
    }

    private static Text CreateUiText(
        string name,
        Transform parent,
        Font font,
        string value,
        int fontSize,
        TextAnchor alignment,
        Vector2 size,
        Vector2 anchoredPosition
    )
    {
        GameObject textObject = new GameObject(name, typeof(RectTransform));
        textObject.transform.SetParent(parent, false);
        RectTransform rect = textObject.GetComponent<RectTransform>();
        rect.anchorMin = new Vector2(0.5f, 0.5f);
        rect.anchorMax = new Vector2(0.5f, 0.5f);
        rect.pivot = new Vector2(0.5f, 0.5f);
        rect.sizeDelta = size;
        rect.anchoredPosition = anchoredPosition;

        Text text = textObject.AddComponent<Text>();
        text.font = font;
        text.text = value;
        text.fontSize = fontSize;
        text.alignment = alignment;
        text.horizontalOverflow = HorizontalWrapMode.Wrap;
        text.verticalOverflow = VerticalWrapMode.Truncate;
        return text;
    }

    private static WheelCollider CreateWheelCollider(
        Transform parent,
        string name,
        Vector3 localPosition
    )
    {
        GameObject wheelObject = new GameObject(name);
        wheelObject.transform.SetParent(parent, false);
        wheelObject.transform.localPosition = localPosition;

        WheelCollider wheel = wheelObject.AddComponent<WheelCollider>();
        wheel.mass = 20f;
        wheel.radius = 0.35f;
        wheel.suspensionDistance = 0.2f;

        JointSpring spring = wheel.suspensionSpring;
        spring.spring = 35000f;
        spring.damper = 4500f;
        spring.targetPosition = 0.5f;
        wheel.suspensionSpring = spring;

        WheelFrictionCurve forwardFriction = wheel.forwardFriction;
        forwardFriction.stiffness = 1.5f;
        wheel.forwardFriction = forwardFriction;

        WheelFrictionCurve sidewaysFriction = wheel.sidewaysFriction;
        sidewaysFriction.stiffness = 2f;
        wheel.sidewaysFriction = sidewaysFriction;

        return wheel;
    }

    private static Transform CreateWheelVisual(
        Transform parent,
        string name,
        Vector3 localPosition,
        Material material
    )
    {
        GameObject pivot = new GameObject(name);
        pivot.transform.SetParent(parent, false);
        pivot.transform.localPosition = localPosition;

        GameObject wheelMesh = GameObject.CreatePrimitive(PrimitiveType.Cylinder);
        wheelMesh.name = "Mesh";
        wheelMesh.transform.SetParent(pivot.transform, false);
        wheelMesh.transform.localRotation = Quaternion.Euler(0f, 0f, 90f);
        wheelMesh.transform.localScale = new Vector3(0.7f, 0.12f, 0.7f);
        wheelMesh.GetComponent<Renderer>().sharedMaterial = material;
        Object.DestroyImmediate(wheelMesh.GetComponent<Collider>());

        return pivot.transform;
    }

    private static void CreateCamera(Transform target)
    {
        GameObject cameraObject = new GameObject("Main Camera");
        Camera camera = cameraObject.AddComponent<Camera>();
        cameraObject.tag = "MainCamera";
        camera.fieldOfView = 62f;
        camera.nearClipPlane = 0.1f;
        camera.farClipPlane = 1000f;

        SimpleFollowCamera followCamera = cameraObject.AddComponent<SimpleFollowCamera>();
        followCamera.target = target;

        cameraObject.transform.position = target.position + new Vector3(0f, 4.2f, -8.5f);
        cameraObject.transform.LookAt(target.position + Vector3.up * 0.8f + target.forward * 5f);
    }

    private static void CreateLighting()
    {
        GameObject lightObject = new GameObject("Directional Light");
        Light light = lightObject.AddComponent<Light>();
        light.type = LightType.Directional;
        light.intensity = 1.1f;
        lightObject.transform.rotation = Quaternion.Euler(48f, -32f, 0f);

        RenderSettings.ambientMode = UnityEngine.Rendering.AmbientMode.Flat;
        RenderSettings.ambientLight = new Color(0.55f, 0.55f, 0.55f);
    }

    private static void CreateSegmentLine(
        string name,
        float x,
        float length,
        Material material,
        Transform parent
    )
    {
        CreateCube(
            name,
            new Vector3(x, 0.012f, 0f),
            new Vector3(0.14f, 0.025f, length),
            material,
            parent,
            false,
            true
        );
    }

    private static GameObject CreateCube(
        string name,
        Vector3 position,
        Vector3 scale,
        Material material,
        Transform parent,
        bool keepCollider,
        bool useLocalCoordinates = false
    )
    {
        GameObject gameObject = GameObject.CreatePrimitive(PrimitiveType.Cube);
        gameObject.name = name;
        gameObject.transform.SetParent(parent, false);

        if (useLocalCoordinates)
        {
            gameObject.transform.localPosition = position;
            gameObject.transform.localScale = scale;
        }
        else
        {
            gameObject.transform.position = position;
            gameObject.transform.localScale = scale;
        }

        gameObject.GetComponent<Renderer>().sharedMaterial = material;

        if (!keepCollider)
        {
            Object.DestroyImmediate(gameObject.GetComponent<Collider>());
        }

        return gameObject;
    }

    private static Material GetOrCreateMaterial(string path, Color color)
    {
        Material material = AssetDatabase.LoadAssetAtPath<Material>(path);
        if (material != null)
        {
            return material;
        }

        Shader shader = Shader.Find("Universal Render Pipeline/Lit");
        if (shader == null)
        {
            shader = Shader.Find("Standard");
        }

        material = new Material(shader);
        material.color = color;
        if (material.HasProperty("_BaseColor"))
        {
            material.SetColor("_BaseColor", color);
        }
        AssetDatabase.CreateAsset(material, path);
        return material;
    }

    private static void EnsureFolder(string path)
    {
        if (AssetDatabase.IsValidFolder(path))
        {
            return;
        }

        string parent = System.IO.Path.GetDirectoryName(path)?.Replace("\\", "/");
        string name = System.IO.Path.GetFileName(path);

        if (!string.IsNullOrEmpty(parent) && !AssetDatabase.IsValidFolder(parent))
        {
            EnsureFolder(parent);
        }

        if (!string.IsNullOrEmpty(parent))
        {
            AssetDatabase.CreateFolder(parent, name);
        }
    }
}
