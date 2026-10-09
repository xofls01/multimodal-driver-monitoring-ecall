using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

public static class BasicDrivingSceneBuilder
{
    private const string RootFolder = "Assets/Stage1BasicVehicle";
    private const string MaterialFolder = RootFolder + "/Materials";
    private const string SceneFolder = RootFolder + "/Scenes";

    [MenuItem("Tools/Emergency Driving Demo/Create Stage 1 Scene")]
    public static void CreateStage1Scene()
    {
        if (!EditorSceneManager.SaveCurrentModifiedScenesIfUserWantsTo())
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

        CreateEnvironment(roadMaterial, shoulderMaterial, lineMaterial);
        BasicWheelVehicleController vehicle = CreateVehicle(
            bodyMaterial,
            windowMaterial,
            wheelMaterial
        );
        UnityTcpVehicleBridge tcpBridge = CreateTcpBridge(vehicle);
        CreateCamera(vehicle.transform);
        CreateLighting();

        BasicVehicleHUD hud = new GameObject("VehicleHUD").AddComponent<BasicVehicleHUD>();
        hud.vehicle = vehicle;
        hud.tcpBridge = tcpBridge;

        string scenePath = SceneFolder + "/BasicDrivingStage1.unity";
        EditorSceneManager.SaveScene(scene, scenePath);
        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();

        Selection.activeGameObject = vehicle.gameObject;
        EditorGUIUtility.PingObject(vehicle.gameObject);

        EditorUtility.DisplayDialog(
            "Stage 1 Scene Created",
            "기본 도로, 갓길, WheelCollider 차량, 추적 카메라와 HUD를 생성했습니다.\n\nPlay 후 W/S, A/D, Space, R 키를 사용하세요.",
            "확인"
        );
    }

    private static UnityTcpVehicleBridge CreateTcpBridge(
        BasicWheelVehicleController vehicle
    )
    {
        GameObject bridgeObject = new GameObject("PythonTcpBridge");
        UnityTcpVehicleBridge bridge = bridgeObject.AddComponent<UnityTcpVehicleBridge>();
        bridge.vehicle = vehicle;
        bridge.host = "127.0.0.1";
        bridge.port = 5001;
        bridge.stateSendRateHz = 20f;
        return bridge;
    }

    private static void CreateEnvironment(
        Material roadMaterial,
        Material shoulderMaterial,
        Material lineMaterial
    )
    {
        GameObject environment = new GameObject("Environment");

        // 일반 도로: x=-6~3, 갓길: x=3~6
        CreateCube(
            "MainRoad",
            new Vector3(-1.5f, -0.1f, 180f),
            new Vector3(9f, 0.2f, 400f),
            roadMaterial,
            environment.transform,
            true
        );
        CreateCube(
            "Shoulder",
            new Vector3(4.5f, -0.1f, 180f),
            new Vector3(3f, 0.2f, 400f),
            shoulderMaterial,
            environment.transform,
            true
        );

        CreateRoadLine("LeftEdge", -5.9f, 180f, 400f, lineMaterial, environment.transform);
        CreateRoadLine("ShoulderBoundary", 3f, 180f, 400f, lineMaterial, environment.transform);
        CreateRoadLine("RightEdge", 5.9f, 180f, 400f, lineMaterial, environment.transform);

        // 두 차선을 구분하는 점선입니다.
        for (float z = -16f; z <= 376f; z += 10f)
        {
            CreateCube(
                "LaneDash",
                new Vector3(-1.5f, 0.012f, z),
                new Vector3(0.12f, 0.025f, 5f),
                lineMaterial,
                environment.transform,
                false
            );
        }

        // 멀리서도 갓길 영역을 알아볼 수 있는 간단한 표지입니다.
        GameObject sign = CreateCube(
            "ShoulderSign",
            new Vector3(4.5f, 1.2f, 35f),
            new Vector3(1.8f, 1.2f, 0.12f),
            lineMaterial,
            environment.transform,
            false
        );
        TextMesh text = new GameObject("Label").AddComponent<TextMesh>();
        text.transform.SetParent(sign.transform, false);
        text.transform.localPosition = new Vector3(0f, 0f, -0.07f);
        text.transform.localRotation = Quaternion.Euler(0f, 180f, 0f);
        text.text = "SHOULDER";
        text.anchor = TextAnchor.MiddleCenter;
        text.alignment = TextAlignment.Center;
        text.characterSize = 0.16f;
        text.fontSize = 48;
        text.color = Color.black;
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

        return controller;
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

    private static void CreateRoadLine(
        string name,
        float x,
        float z,
        float length,
        Material material,
        Transform parent
    )
    {
        CreateCube(
            name,
            new Vector3(x, 0.012f, z),
            new Vector3(0.14f, 0.025f, length),
            material,
            parent,
            false
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
