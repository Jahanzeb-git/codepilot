import httpx
import asyncio
import os
from dotenv import load_dotenv
import logging

load_dotenv()
logger = logging.getLogger(__name__)

FLY_API_TOKEN = os.environ.get("FLY_API_TOKEN")
APP_NAME = os.environ.get("APP_NAME")
WORKSPACE_APP_NAME = os.environ.get("WORKSPACE_APP_NAME")
RUNNER_IMAGE = os.environ.get("RUNNER_IMAGE")


class MachineService:

    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url = "https://api.machines.dev/v1",
            limits = httpx.Limits(max_connections=20, max_keepalive_connections=10),
            timeout = httpx.Timeout(60.0),
            headers = {"Authorization": f"Bearer {FLY_API_TOKEN}"}
        )
    
    async def create_machine(self, machine_secret: str, workspace_id: str):
        payload = {
            "config": {
                "image": RUNNER_IMAGE,
                "guest": {
                    "cpu_kind": "shared",
                    "cpus": 2,
                    "memory_mb": 2048
                },
                "env": {
                    "MACHINE_SECRET": machine_secret,
                    "WORKSPACE_ID": workspace_id,
                    "CREDENTIALS_ENDPOINT": "https://codepilot-api.fly.dev/machines/internal/b2-credentials",
                    "CONTROL_PLANE_URL": "https://codepilot-api.fly.dev",
                    "EXPERIENTIAL_API_KEY": os.environ.get("EXPERIENTIAL_API_KEY", ""),
                    "VOYAGE_API_KEY": os.environ.get("VOYAGE_API_KEY", ""),
                    "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", "")
                },
                "services": [
                    {
                        "protocol": "tcp",
                        "internal_port": 8080,
                        "ports": [
                            {
                                "port": 80,
                                "handlers": ["http"]
                            },
                            {
                                "port": 443,
                                "handlers": ["tls", "http"]
                            }
                        ],
                        "checks": [
                            {
                                "type": "tcp",
                                "interval": "5s",
                                "timeout": "2s",
                                "grace_period": "5s"
                            }
                        ]
                    }
                ],
                "metadata": {
                    "fly_platform_version": "v2",
                    "fly_process_group": "app"
                }
            },
            "region": "sin"
        }
        try:
            response = await self.client.post(f"/apps/{WORKSPACE_APP_NAME}/machines", json=payload)
            response.raise_for_status()
            return response.json() 

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while creating machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while creating machine.")
            raise
            

    async def start_machine(self, machine_id: str):
        try:
            response = await self.client.post(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}/start"
            )
            response.raise_for_status()
            return response.json()
        
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while starting machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while starting machine.")
            raise



    async def restart_machine(self, machine_id: str):
        try: 
            response = await self.client.post(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}/restart"
            )
            response.raise_for_status()
            return response.json()
        
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while restarting machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while restarting machine.")
            raise

    async def destroy_machine(self, machine_id: str):
        try: 
            response = await self.client.delete(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}"
            )
            response.raise_for_status()

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while deleting machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while deleting machine.")
            raise


    async def update_machine_image_if_needed(self, machine_id: str):
        try:
            # 1. Fetch current machine state
            machine_data = await self.check_status(machine_id)
            current_config = machine_data.get("config", {})
            current_image = current_config.get("image", "")

            # 2. Check if image matches the desired RUNNER_IMAGE
            if RUNNER_IMAGE and current_image != RUNNER_IMAGE:
                logger.info(f"Machine {machine_id} is running outdated image '{current_image}'. Updating to '{RUNNER_IMAGE}'...")
                
                # Update the image in the config payload
                current_config["image"] = RUNNER_IMAGE
                
                # Also ensure the env dictionary has all required keys in case they were added since the machine was created
                current_env = current_config.setdefault("env", {})
                current_env["EXPERIENTIAL_API_KEY"] = os.environ.get("EXPERIENTIAL_API_KEY", "")
                current_env["DEEPSEEK_API_KEY"] = os.environ.get("DEEPSEEK_API_KEY", "")
                current_env["VOYAGE_API_KEY"] = os.environ.get("VOYAGE_API_KEY", "")
                current_env["TAVILY_API_KEY"] = os.environ.get("TAVILY_API_KEY", "")
                
                payload = {"config": current_config}
                
                # 3. Post the updated config back to Fly.io
                response = await self.client.post(
                    f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}", 
                    json=payload
                )
                response.raise_for_status()
                logger.info(f"Successfully updated image for machine {machine_id}.")
                return response.json()
            else:
                logger.info(f"Machine {machine_id} is already running the latest image '{RUNNER_IMAGE}'.")
                return None

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while updating machine image. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except Exception as e:
            logger.error(f"Unexpected error while updating machine image: {e}")
            raise

    async def check_status(self, machine_id: str):
        try:
            response = await self.client.get(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}"
            )
            response.raise_for_status()
            return response.json()
        
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while checking machine status. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while checking machine status.")
            raise

    async def stop_machine(self, machine_id: str): 
        try:
            response = await self.client.post(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}/stop"
            )
            response.raise_for_status()
            return response.json()
        
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while stopping machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while stopping machine.")
            raise

    async def suspend_machine(self, machine_id: str):
        try:
            response = await self.client.post(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}/suspend"
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while suspending machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while suspending machine.")
            raise


    async def delete_machine(self, machine_id: str):
        try:
            response = await self.client.delete(
                f"/apps/{WORKSPACE_APP_NAME}/machines/{machine_id}?force=true"
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error while deleting machine. Status: {e.response.status_code}, Response: {e.response.text}")
            raise
        
        except httpx.ConnectError:
            logger.error("Failed to connect to Fly Machines API.")
            raise

        except httpx.TimeoutException: 
            logger.error("Request to Fly Machines API timed out.")
            raise

        except httpx.RequestError as e: 
            logger.error(f"Request error: {e}")
            raise

        except Exception as e: 
            logger.error("Unexpected error while deleting machine.")
            raise
        