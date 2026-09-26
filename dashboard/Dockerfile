FROM node:22-alpine AS build
WORKDIR /app
# the lockfile CI audited and tested, not whatever `npm install` resolves today
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
