FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24

COPY app/requirements.txt /app/requirements.txt
# gcc is only needed if a wheel is missing for this Python/arch; removed afterwards.
RUN apk add --no-cache --virtual .build-deps gcc musl-dev \
    && pip3 install --no-cache-dir -r /app/requirements.txt \
    && apk del .build-deps

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
