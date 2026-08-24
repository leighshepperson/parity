FROM python:3.12-slim-bookworm AS parity-build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --prefix=/install .

FROM python:3.12-slim-bookworm AS fortran-build

RUN apt-get update \
    && apt-get install --yes --no-install-recommends gfortran \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY case_studies/fortran_python/reference.f90 ./reference.f90
RUN gfortran --version \
    && mkdir /out \
    && gfortran -std=f2008 -O0 -o /out/compensated_sum reference.f90

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends libgfortran5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 parity \
    && useradd --create-home --gid 10001 --uid 10001 parity

COPY --from=parity-build /install/ /usr/local/
COPY --chown=10001:10001 case_studies/fortran_python/ /opt/proof/
COPY --from=fortran-build --chown=10001:10001 \
    /out/compensated_sum /opt/proof/bin/compensated_sum

RUN test ! -e /usr/bin/gfortran \
    && ! command -v docker \
    && python -c "import parity, pyarrow"

USER 10001:10001
WORKDIR /opt/proof

ENTRYPOINT ["python", "verify.py"]
