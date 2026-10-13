"""Reintenta crear la VM Always Free A1 (watchdog-hv) en sa-bogota-1 hasta que haya cupo.
Corre en GitHub Actions (repo PUBLICO: no imprimir IP ni OCIDs en el log; la IP va solo por ntfy).
Salida: 0 = VM creada (o ya existia), 3 = se acabo el tiempo sin cupo (re-lanzar), 1 = error real."""
import os, sys, time, urllib.request
import oci

NAME = "watchdog-hv"
IMAGE_NAME = "Canonical-Ubuntu-24.04-aarch64"  # la mas reciente que coincida
SIZES = [(4, 24), (2, 12), (1, 6)]  # del mas grande al mas chico; despues se puede agrandar
CALL_PAUSE = 60      # entre intentos (el API da 429 si se va mas rapido)
DEADLINE = time.time() + int(os.environ.get("MAX_MINUTES", "330")) * 60

cfg = {
    "user": os.environ["OCI_USER"],
    "fingerprint": os.environ["OCI_FINGERPRINT"],
    "tenancy": os.environ["OCI_TENANCY"],
    "region": "sa-bogota-1",
    "key_content": os.environ["OCI_KEY"],
}
oci.config.validate_config(cfg)
T = cfg["tenancy"]
compute = oci.core.ComputeClient(cfg)
net = oci.core.VirtualNetworkClient(cfg)


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def notify(title, msg, prio="high"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(),
                                     headers={"Title": title, "Priority": prio})
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        log(f"ntfy fallo: {type(e).__name__}")


def existing():
    for i in compute.list_instances(T, display_name=NAME).data:
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return i
    return None


def image_id():
    imgs = compute.list_images(T, operating_system="Canonical Ubuntu", shape="VM.Standard.A1.Flex",
                               sort_by="TIMECREATED", sort_order="DESC").data
    return next(i.id for i in imgs if i.display_name.startswith(IMAGE_NAME) and "Minimal" not in i.display_name)


def launch(ad, img, ocpus, mem):
    d = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad, compartment_id=T, display_name=NAME,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=mem),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=img),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=os.environ["OCI_SUBNET"], assign_public_ip=True, display_name="watchdog-vnic"),
        metadata={"ssh_authorized_keys": os.environ["SSH_PUB"]},
    )
    return compute.launch_instance(d).data


def public_ip(inst):
    for va in compute.list_vnic_attachments(T, instance_id=inst.id).data:
        if va.vnic_id:
            ip = net.get_vnic(va.vnic_id).data.public_ip
            if ip:
                return ip
    return None


def finish(inst, ocpus=None):
    inst = oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING",
                          max_wait_seconds=900).data
    ip = None
    for _ in range(30):
        ip = public_ip(inst)
        if ip:
            break
        time.sleep(10)
    log("LISTO: VM RUNNING (IP enviada por ntfy)")
    size = f" con {ocpus} OCPU" if ocpus else ""
    notify("VM Oracle creada", f"watchdog-hv RUNNING{size} - IP {ip}. Avisale a Claude.", "urgent")


def main():
    inst = existing()
    if inst:
        log(f"Ya existe la VM ({inst.lifecycle_state}); no se crea otra.")
        return 0
    ad = oci.identity.IdentityClient(cfg).list_availability_domains(T).data[0].name
    img = image_id()
    tries = 0
    while time.time() < DEADLINE:
        for ocpus, mem in SIZES:
            tries += 1
            try:
                inst = launch(ad, img, ocpus, mem)
                log(f"Creada con {ocpus} OCPU / {mem} GB en el intento {tries}")
                finish(inst, ocpus)
                return 0
            except oci.exceptions.ServiceError as e:
                if e.status == 429:
                    log("429 TooManyRequests; espero mas")
                    time.sleep(120)
                elif "capacity" in (e.message or "").lower():
                    log(f"intento {tries}: sin cupo {ocpus}/{mem}")
                else:
                    log(f"ERROR no esperado {e.status} {e.code}")
                    notify("Retry VM Oracle: error", f"{e.status} {e.code}: {e.message}"[:300])
                    return 1
            except Exception as e:
                log(f"error de red: {type(e).__name__}")
            time.sleep(CALL_PAUSE)
    log(f"Sin cupo tras {tries} intentos; se re-lanza el workflow.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
