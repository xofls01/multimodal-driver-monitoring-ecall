using UnityEngine;

public class EndlessRoadController : MonoBehaviour
{
    [Header("References")]
    public Transform vehicle;
    public Transform[] roadSegments;

    [Header("Recycling")]
    [Min(10f)] public float segmentLength = 100f;
    [Min(0f)] public float recycleBehindDistance = 20f;

    public int SegmentCount => roadSegments == null ? 0 : roadSegments.Length;

    private Vector3[] initialSegmentPositions;
    private float lastVehicleZ;

    private void Awake()
    {
        if (roadSegments != null)
        {
            initialSegmentPositions = new Vector3[roadSegments.Length];
            for (int index = 0; index < roadSegments.Length; index++)
            {
                if (roadSegments[index] != null)
                {
                    initialSegmentPositions[index] = roadSegments[index].position;
                }
            }
        }
        lastVehicleZ = vehicle == null ? 0f : vehicle.position.z;
    }

    private void LateUpdate()
    {
        if (vehicle == null || roadSegments == null || roadSegments.Length < 2)
        {
            return;
        }

        // R 초기화처럼 물리 주행으로는 나오기 어려운 큰 후방 순간이동을 감지합니다.
        if (vehicle.position.z < lastVehicleZ - segmentLength * 0.1f)
        {
            ResetRoadSegments();
        }
        lastVehicleZ = vehicle.position.z;

        // 한 프레임에 여러 구간을 건너뛴 경우도 고정 배열 안에서 따라잡습니다.
        for (int iteration = 0; iteration < roadSegments.Length; iteration++)
        {
            Transform rearmost = null;
            float rearmostZ = float.PositiveInfinity;
            float foremostZ = float.NegativeInfinity;

            foreach (Transform segment in roadSegments)
            {
                if (segment == null)
                {
                    continue;
                }

                if (segment.position.z < rearmostZ)
                {
                    rearmost = segment;
                    rearmostZ = segment.position.z;
                }
                foremostZ = Mathf.Max(foremostZ, segment.position.z);
            }

            if (rearmost == null
                || rearmostZ + segmentLength * 0.5f
                    >= vehicle.position.z - recycleBehindDistance)
            {
                break;
            }

            Vector3 nextPosition = rearmost.position;
            nextPosition.z = foremostZ + segmentLength;
            rearmost.position = nextPosition;
        }
    }

    public void ResetRoadSegments()
    {
        if (roadSegments == null || initialSegmentPositions == null)
        {
            return;
        }

        for (int index = 0; index < roadSegments.Length; index++)
        {
            if (roadSegments[index] != null && index < initialSegmentPositions.Length)
            {
                roadSegments[index].position = initialSegmentPositions[index];
            }
        }
    }
}
