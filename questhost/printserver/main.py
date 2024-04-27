import base64
import logging
from pathlib import Path
import re
from tempfile import TemporaryFile
import time
from subprocess import Popen, PIPE
from fastapi import FastAPI, File, HTTPException, UploadFile
import imgkit
from io import BytesIO
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2.exceptions import TemplateNotFound
import qrcode
from PIL import Image
from pydantic import BaseModel


class PrintParameters(BaseModel):
    template: str
    data: dict


logging.basicConfig(level=logging.INFO)
app = FastAPI()
app_template_path = Path("~/.questhost/templates").expanduser().resolve()
app_template_path.mkdir(parents=True, exist_ok=True)
print(f"Templates are saved in: {app_template_path}")
loader = FileSystemLoader(app_template_path)
env = Environment(loader=loader, autoescape=select_autoescape())


def __execute_ptouch(args=[], max_retries=50) -> tuple[str, str]:
    if not args:
        args = ["--info"]
    
    for _ in range(50):
        result: tuple[str,str]= tuple(
            x.decode().replace("\n", "")
            for x in Popen(
                f"ptouch-print {' '.join(args)}",
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
            ).communicate()
        )
        if any('timeout' in x.lower() for x in result):
            logging.warning("ptouch-print return `timeout`")
            for returns in result:
                logging.info(f"\t* {returns}")
            time.sleep(0.5)
        else:
            return result
    raise TimeoutError()    


@app.get("/health/")
def health() -> bool:
    """Check if various hardware/software are properly setup"""
    stdout, stderr = __execute_ptouch()
    # TODO: Need to check more?
    # TODO: Check wkhtmltopdf?
    if "px" not in stdout or stderr:
        return False
    return True


@app.post("/print/ptouch/", responses={400: {}, 500: {}})
async def print_ptouch(print_param: PrintParameters) -> bool:
    """Print provided `data` as kwargs to the `template`.

    Please note that `QRCODE` will be generated from the `data['id']`, so
    it is required to be provided.
    """
    try:
        buff = BytesIO()
        qrimg = qrcode.make(print_param.data["id"])
        qrimg.save(buff, format="PNG")
        kwargs = dict(print_param.data)
        kwargs["QRCODE"] = (
            "data:image/png;base64,"
            + base64.b64encode(buff.getvalue()).decode()  # noqa: E501
        )

        template = env.get_template(print_param.template)
        html_result = template.render(kwargs)
        with TemporaryFile("wb") as TEMPFILE:
            imgkit.from_string(html_result, output_path=f"{TEMPFILE.name}.png")
            logging.info(f"Generated ticket to be printed: '{TEMPFILE.name}.png'")

            is_resized = False
            for _ in range(10):
                stdout, stderr = __execute_ptouch()
                regex = re.search(r"(?P<MAXWIDTH>\d+)px", stdout)
                if not regex:
                    logging.warning(f"No width. STDOUT: {stdout}, STDERR: {stderr}")
                    time.sleep(0.5)
                    continue
                max_width = int(regex.group("MAXWIDTH"))
                logging.info(f"Max width: {max_width}")
                nonscale = Image.open(f"{TEMPFILE.name}.png")
                ratio = max_width / nonscale.size[1]
                nonscale.resize(
                    (int(nonscale.size[0] * ratio), int(nonscale.size[1] * ratio))
                ).save(f"{TEMPFILE.name}.png")
                is_resized = True
                break

            if not is_resized:
                raise TimeoutError("Cannot resize image")

            is_printed = False
            for _ in range(10):
                stdout, stderr = __execute_ptouch(["--image", f"{TEMPFILE.name}.png"])
                is_printed = True
                break

            if not is_resized:
                raise TimeoutError("Cannot print image")

            Path(f"{TEMPFILE.name}.png").unlink()
            return is_printed and is_resized
    except (TemplateNotFound, KeyError) as e:
        raise HTTPException(400, str(e))
    except TimeoutError as e:
        raise HTTPException(500, f"Unable to finish command:\n{str(e)}")


@app.post("/upload/")
async def upload(file: UploadFile = File()) -> bool:
    template_path = app_template_path.joinpath(file.filename)
    with open(template_path, "wb") as FILE:
        FILE.write(file.file.read())
    return True
