#!/bin/bash
# Executed inside a single customer-owned RHEL guest by systemd.
set -euo pipefail
exec > >(tee -a /var/log/edu-inference.log) 2>&1
source /etc/edu-inference.env
stage() { echo "EDU-RHEL: $*"; }
trap 'echo "EDU-RHEL: bootstrap failed at line $LINENO; systemd will retry"' ERR
stage "Checking RHEL CPU and storage"
test -f /etc/redhat-release
grep -qw avx2 /proc/cpuinfo
mkdir -p /var/lib/edu-inference/model
cd /var/lib/edu-inference
if ! command -v podman >/dev/null; then
    stage "Installing Red Hat Podman from signed UBI 9 repositories"
    dnf_cmd=(dnf -y --disablerepo='*' --disableplugin=subscription-manager \
      --repofrompath=edu-baseos,https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/baseos/os/ \
      --repofrompath=edu-appstream,https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/appstream/os/ \
      --enablerepo=edu-baseos,edu-appstream --setopt=install_weak_deps=False \
      --setopt=edu-baseos.gpgcheck=True --setopt=edu-appstream.gpgcheck=True \
      --setopt=edu-baseos.gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release \
      --setopt=edu-appstream.gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release --setopt=localpkg_gpgcheck=True)
    if ! "${dnf_cmd[@]}" install podman; then
        # The UBI9 repository can advertise netavark epoch 3 alongside aardvark epoch 2.
        # This signed Red Hat netavark also 1.17.2 requires the available epoch-2 aardvark.
        stage "Installing compatible signed UBI netavark 1.17.2-1"
        curl --fail --silent --show-error --location --retry 3 \
          https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/appstream/os/Packages/n/netavark-1.17.2-1.el9.x86_64.rpm -o netavark.rpm
        echo '1bf196b5daa0aa92bd1408418773d70eadbc15442ec8b7f4b91a7be0b41cf693  netavark.rpm' | sha256sum -c -
        rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-redhat-release
        rpmkeys --checksig netavark.rpm
        "${dnf_cmd[@]}" install ./netavark.rpm podman
        rm -f netavark.rpm
    fi
fi
if ! podman image exists localhost/edu-rhel-inference:3.5.0; then
    stage "Waiting for customer image archive"
    curl --show-error --fail --silent --location --connect-timeout 10 --max-time 1800 \
      "$IMAGE_URL" -o image.tar.part
    expected=$(curl --fail --silent --show-error "${IMAGE_URL}.sha256" | cut -d' ' -f1)
    [[ "$expected" =~ ^[[:xdigit:]]{64}$ ]]
    echo "$expected  image.tar.part" | sha256sum -c -
    stage "Loading the CPU inference image"
    podman load -i image.tar.part
    podman image exists localhost/edu-rhel-inference:3.5.0
    rm -f image.tar.part
fi
revision=7ae557604adf67be50417f59c2c2f167def9a775
stage "Loading pinned public Qwen model"
for file in config.json generation_config.json merges.txt model.safetensors tokenizer.json tokenizer_config.json vocab.json LICENSE; do
    if ! test -s "model/$file"; then
        curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 900 \
          "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/resolve/$revision/$file" -o "model/$file.part"
        if [ "$file" = model.safetensors ]; then
            echo 'fdf756fa7fcbe7404d5c60e26bff1a0c8b8aa1f72ced49e7dd0210fe288fb7fe  model/model.safetensors.part' | sha256sum -c -
        fi
        mv -f "model/$file.part" "model/$file"
    fi
done
chmod -R a+rX model
stage "Starting the Red Hat AI Inference service"
exec podman run --name edu-inference --replace --rm --pull=never --shm-size=2g \
  --env VLLM_CPU_KVCACHE_SPACE=2 --env LD_PRELOAD=/usr/lib64/libomp.so \
  --env HF_HUB_OFFLINE=1 --env OMP_NUM_THREADS="$INFERENCE_THREADS" \
  --publish 8000:8000 --volume /var/lib/edu-inference/model:/data/model:ro,Z \
  localhost/edu-rhel-inference:3.5.0 --model /data/model --served-model-name Qwen/Qwen2.5-0.5B-Instruct \
  --max-model-len 8192 --max-num-seqs 2 --dtype float32 --enforce-eager
