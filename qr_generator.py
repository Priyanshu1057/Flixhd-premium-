import qrcode
import io
import logging
from urllib.parse import quote
from config import UPI_ID

logger = logging.getLogger(__name__)


def generate_upi_qr(amount: int, service_name: str, plan_label: str, order_id: str) -> io.BytesIO:
    upi_id_clean = UPI_ID.strip()
    note = f"{service_name} - {plan_label}"

    upi_url = (
        f"upi://pay"
        f"?pa={upi_id_clean}"
        f"&pn=PremiumBot"
        f"&am={amount}.00"
        f"&cu=INR"
        f"&tn={quote(note, safe='')}"
    )

    logger.info(f"Generating UPI QR for pa={upi_id_clean} am={amount}")

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=20,
        border=6,
    )
    qr.add_data(upi_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
