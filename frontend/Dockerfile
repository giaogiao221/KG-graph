# syntax=docker/dockerfile:1
# Frontend image: serve the pre-built Vue application through nginx with the
# API/SSE proxy from deploy/nginx.conf.
# Build context is the repository root: docker build -f frontend/Dockerfile .
#
# The Vue app is built locally with `npm run build` (Node 22+); the resulting
# dist/ is copied directly. This avoids npm platform-specific optional
# dependency failures inside the Alpine build stage.
FROM nginx:1.27-alpine AS runtime

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/dist /usr/share/nginx/html

EXPOSE 80

