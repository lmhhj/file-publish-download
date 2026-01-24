FROM nginx:alpine

# 将你的 Nginx 配置文件复制进去
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 将公共端和管理端静态文件复制进去
COPY public /usr/share/nginx/html
COPY admin /usr/share/nginx/html/admin

EXPOSE 80
