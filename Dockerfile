FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

# Xvfb + noVNC ficam DENTRO desta imagem de propósito: o login das contas do
# ChatGPT só passa à mão (Cloudflare + login Google), e depender do navegador
# do hubbsfield para isso já se mostrou frágil. Aqui o relogin é local.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils x11vnc fluxbox novnc websockify python3-numpy \
        xdotool fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --ignore-installed typing_extensions \
    && pip install --no-cache-dir \
       playwright==1.61.0 fastapi==0.115.6 uvicorn==0.34.0 httpx==0.28.1

WORKDIR /app
COPY app/ app/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# 3000 = API OpenAI-compatible (Traefik)   8080 = noVNC (só por túnel SSH)
EXPOSE 3000 8080
CMD ["./entrypoint.sh"]
