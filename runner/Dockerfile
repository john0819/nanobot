FROM python:3.13.4-slim@sha256:9ed09f78253eb4f029f3d99e07c064f138a6f1394932c3807b3d0738a674d33b
RUN python -m pip install --no-cache-dir pytest==9.1.1 iniconfig==2.3.0 packaging==26.3 pluggy==1.6.0 pygments==2.21.0
WORKDIR /suite
COPY testpilot/fixtures/gateway.py /suite/fixed_fixture/gateway.py
COPY testpilot/fixtures/suite.py /suite/fixed_fixture/test_gateway.py
COPY runner/entry.py /suite/entry.py
RUN touch /suite/fixed_fixture/__init__.py && useradd --uid 10001 --no-create-home runner
USER 10001:10001
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-I", "/suite/entry.py"]
