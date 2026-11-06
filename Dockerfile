FROM node:22-slim
RUN apt-get update && apt-get install -y git build-essential curl clang \
	pkgconf python3-cryptography procps gdb strace
RUN npm install -g @anthropic-ai/claude-code
RUN adduser --uid 1023 --gid 100 --disabled-password claude
USER claude
CMD ["claude"]
