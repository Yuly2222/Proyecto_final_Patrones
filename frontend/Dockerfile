FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci: instala EXACTAMENTE el lockfile (versiones + hash de integridad).
# --ignore-scripts: ningún paquete puede ejecutar código en postinstall (vector típico de supply chain).
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
