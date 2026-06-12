# DataForge console image — multi-target per deployment-architecture §8.2:
#   deps  → npm ci (shared dependency layer)
#   dev   → Vite dev server (HMR; compose `web` service bind-mounts ../../frontend:/app
#           with an anonymous volume over node_modules)
#   build → tsc && vite build → /app/dist (CI artifact stage; output feeds the backend
#           `runtime` image — there is no production frontend container)

FROM node:22-alpine AS deps
# curl is required by the compose healthcheck: `curl -fsS localhost:5173/`
RUN apk add --no-cache curl
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS dev
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

FROM deps AS build
COPY . .
RUN npm run build
