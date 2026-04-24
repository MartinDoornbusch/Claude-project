FROM python:3.12-alpine

# Build dependencies voor pandas en andere C-extensies
RUN apk add --no-cache gcc musl-dev python3-dev libffi-dev

# Installeer Python packages tijdens image build
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
