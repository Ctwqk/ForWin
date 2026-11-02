# ssh client that forwards publish-hub (magi2:18930, through the VPS) into ForWin's compose
# network. Base: the alpine image already on magi3 (Docker Hub is unreachable from there),
# pinned by digest.
FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

# Host name of an Alpine mirror reachable from magi3; dl-cdn.alpinelinux.org is not.
ARG ALPINE_MIRROR="mirrors.aliyun.com"
RUN if [ -n "$ALPINE_MIRROR" ]; then sed -i "s#dl-cdn.alpinelinux.org#${ALPINE_MIRROR}#" /etc/apk/repositories; fi \
    && apk add --no-cache openssh-client

COPY run.sh /usr/local/bin/publish-hub-tunnel
ENTRYPOINT ["/usr/local/bin/publish-hub-tunnel"]
