import os
import dotenv
import boto3
from dotenv import load_dotenv

# Load .env file content into the program
load_dotenv()

client = boto3.client('bedrock-runtime')

# Get dynamic input from the user in the terminal
user_prompt = input("Enter your prompt: ")

response = client.converse(
    modelId="mistral.mistral-7b-instruct-v0:2",
    messages=[
        {
            "role": "user",
            "content": [{"text": user_prompt}]
        }
    ],
    inferenceConfig={"temperature": 0.7, "maxTokens": 100}
)

# Extract and print just the model's text reply
model_reply = response['output']['message']['content'][0]['text']
print("\nModel Response:", model_reply)