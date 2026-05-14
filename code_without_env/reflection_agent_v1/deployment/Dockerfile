FROM ubuntu:24.04

# Install python3 and required packages
RUN apt-get update && apt-get install  -y --no-install-recommends python3 python3-pip iputils-ping traceroute netcat-traditional libaio1t64 unzip && apt autoremove -y --purge && \
    rm -rf \
    /usr/local/src/* && rm \
    -rf /var/lib/apt/lists/* && rm -rf /usr/share/man/*


# create directory /agent/runner to store code   
RUN mkdir -p /agent/runner

# Copy source files
COPY runner /agent/runner

# Install all Python dependencies from requirements.txt (already copied with runner folder)
RUN pip3 install --break-system-packages --no-cache-dir -r /agent/runner/requirements.txt


COPY entry_point.sh /

RUN chmod +x /entry_point.sh

# Run the web service on container startup. Here we use the gunicorn
# webserver, with one worker process and 8 threads.
ENTRYPOINT ["/entry_point.sh"]
