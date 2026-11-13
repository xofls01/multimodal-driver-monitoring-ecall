using UnityEngine;

public class SimpleFollowCamera : MonoBehaviour
{
    public Transform target;
    public Vector3 localOffset = new Vector3(0f, 4.2f, -8.5f);
    public float positionSmoothTime = 0.12f;
    public float rotationSpeed = 7f;
    public float lookAheadDistance = 5f;
    public float lookHeight = 0.8f;

    private Vector3 positionVelocity;

    private void LateUpdate()
    {
        if (target == null)
        {
            return;
        }

        Vector3 desiredPosition = target.TransformPoint(localOffset);
        transform.position = Vector3.SmoothDamp(
            transform.position,
            desiredPosition,
            ref positionVelocity,
            positionSmoothTime
        );

        Vector3 lookTarget =
            target.position + target.forward * lookAheadDistance + Vector3.up * lookHeight;
        Quaternion desiredRotation = Quaternion.LookRotation(
            lookTarget - transform.position,
            Vector3.up
        );
        transform.rotation = Quaternion.Slerp(
            transform.rotation,
            desiredRotation,
            rotationSpeed * Time.deltaTime
        );
    }
}
