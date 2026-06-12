# CENTRI shell — static SPA served by nginx. Multi-arch (arm64 + amd64):
# both node:slim and nginx:alpine publish arm64 + amd64 manifests.
# The backend URL is a runtime setting (Settings -> Backend URL, stored in
# localStorage; defaults to http://127.0.0.1:8760), so nothing about the
# backend is baked in at build time.
FROM node:20-slim AS build
WORKDIR /shell
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /shell/dist /usr/share/nginx/html
# SPA fallback: route unknown paths to index.html.
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  location / {\n    try_files $uri $uri/ /index.html;\n  }\n}\n' \
    > /etc/nginx/conf.d/default.conf
EXPOSE 80
