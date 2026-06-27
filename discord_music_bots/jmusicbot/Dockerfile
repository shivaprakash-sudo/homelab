FROM eclipse-temurin:17-jre-alpine
WORKDIR /app
RUN wget -q $(wget -qO- https://api.github.com/repos/jagrosh/MusicBot/releases/latest | grep browser_download_url | grep .jar | cut -d '"' -f 4) -O JMusicBot.jar
COPY config.txt .
CMD ["java", "-Dnogui=true", "-jar", "JMusicBot.jar"]