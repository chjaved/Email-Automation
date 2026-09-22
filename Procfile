web: if [ "$SERVICE_ROLE" = "worker" ]; then python main.py run; elif [ "$SERVICE_ROLE" = "worker-paused" ]; then python -c "import time; print('Atlas worker paused pending mailbox verification', flush=True); time.sleep(31536000)"; else python -c "from dashboard import start_dashboard; start_dashboard()"; fi
worker: python main.py run
