FROM apache/airflow:2.10.5

USER root

# 1. Cài đặt Java và các công cụ cần thiết
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Thiết lập biến môi trường
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV SPARK_VERSION=3.3.2
ENV HADOOP_VERSION=3
ENV SPARK_HOME=/opt/spark
ENV PATH="${SPARK_HOME}/bin:${PATH}"
ENV PYSPARK_PYTHON=python3
ENV PYSPARK_DRIVER_PYTHON=python3

# 3. Tải Spark với cơ chế RETRY để tránh lỗi Exit Code 56
# Thêm --retry 5 và --connect-timeout để chịu lỗi mạng tốt hơn
RUN curl -L --retry 5 --retry-delay 10 --connect-timeout 60 \
    https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}.tgz \
    -o /tmp/spark.tgz && \
    tar -xf /tmp/spark.tgz -C /opt && \
    mv /opt/spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION} ${SPARK_HOME} && \
    rm /tmp/spark.tgz

# 4. Tải các thư viện JDBC
# Tách riêng lệnh để nếu 1 file lỗi thì không phải tải lại toàn bộ Spark
RUN curl -L --retry 5 -o ${SPARK_HOME}/jars/sqlite-jdbc-3.46.0.0.jar \
    https://repo1.maven.org/maven2/org/xerial/sqlite-jdbc/3.46.0.0/sqlite-jdbc-3.46.0.0.jar && \
    curl -L --retry 5 -o ${SPARK_HOME}/jars/clickhouse-jdbc-0.6.3-all.jar \
    https://repo1.maven.org/maven2/com/clickhouse/clickhouse-jdbc/0.6.3/clickhouse-jdbc-0.6.3-all.jar

# 5. Phân quyền
RUN chown -R airflow:root ${SPARK_HOME}

USER airflow

# 6. Cài đặt Airflow Provider
RUN pip install --no-cache-dir apache-airflow-providers-apache-spark==4.11.1