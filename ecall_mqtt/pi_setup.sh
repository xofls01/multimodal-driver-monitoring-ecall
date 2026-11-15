#!/bin/bash
# 라즈베리파이에서 한 번 실행: bash ~/ecall_mqtt/pi_setup.sh   (중간에 sudo 비밀번호를 물어봄)
#  1) Mosquitto(브로커) + paho-mqtt 설치
#  2) 같은 공유기(LAN)의 PC 가 1883 포트로 접속할 수 있게 lan.conf 적용 (아이디·비밀번호 없음: 시연용)
#  3) ecall_service.py 를 systemd 서비스로 등록 → Pi 를 켜면 자동 실행, 죽으면 3초 뒤 재시작
set -e
cd "$(dirname "$0")"
DIR="$PWD"

echo "== 1. 패키지 설치"
sudo apt-get update
sudo apt-get install -y mosquitto mosquitto-clients python3-paho-mqtt

echo "== 2. Mosquitto LAN 설정"
sudo cp lan.conf /etc/mosquitto/conf.d/lan.conf
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto

echo "== 3. e-Call 서비스 등록"
sed "s|__DIR__|$DIR|g; s|__USER__|$USER|g" ecall-service.service \
  | sudo tee /etc/systemd/system/ecall-service.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable ecall-service
sudo systemctl restart ecall-service
sleep 2

echo "== 상태"
systemctl is-active mosquitto ecall-service
journalctl -u ecall-service -n 5 --no-pager
echo "IP: $(hostname -I)"
echo "완료. PC 에서 시험: py send_test_trigger.py --broker <위 IP>"
