FROM node:22-slim

ENV NODE_ENV=production FETCHER_DATA_DIR=/data
WORKDIR /srv
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY proto ./proto
COPY src ./src

RUN mkdir /data && chown node:node /data
USER node
VOLUME /data
EXPOSE 8001

# Healthy = the HTTP service answers; whether Steam/GC are ready is in the /health body.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD node -e "fetch('http://127.0.0.1:8001/health').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"

CMD ["node", "src/server.js"]
