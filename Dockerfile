FROM crpi-ubf32fnvh1oxdpf6.cn-hangzhou.personal.cr.aliyuncs.com/syzhao/debian:13
RUN apt update && apt-get install -y vim nginx python3-full python3-pip nodejs npm && apt clean
RUN pip install fastapi kubernetes uvicorn websockets --break-system-packages && pip cache purge
COPY ./ /home/
COPY ./config/default.conf /etc/nginx/conf.d/default.conf
RUN cd /home/traefik-dashboard/ && npm install && npm run build
WORKDIR /home
ENTRYPOINT ["bash","-c","nginx && python3 main.py"]
