from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import stripe
import os
from dotenv import load_dotenv 
from supabase import create_client, Client
import json

load_dotenv()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
webhook_secret = os.getenv('STRIPE_WEBHOOK_SIGNING_SECRET') 

supabase_url = os.getenv('SUPABASE_URL')
supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
supabase: Client = create_client(supabase_url, supabase_key)


app = FastAPI()

@app.get("/")
async def read_root():
    return {"message": "Backend is running"}

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body() 
    sig_header = request.headers.get('Stripe-Signature') 

    if not webhook_secret:
         print("Webhook signing secret not configured!")
         raise HTTPException(status_code=500, detail="Webhook signing secret not configured.")


    event = None 

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, webhook_secret
        )
    except ValueError as e:
        print(f"Webhook Error: Invalid payload - {e}")
        return JSONResponse(content={"detail": "Invalid payload"}, status_code=400) # Devuelve 400 para indicar a Stripe que no reintente

    except stripe.error.SignatureVerificationError as e:
        print(f"Webhook Error: Invalid signature - {e}")
        return JSONResponse(content={"detail": "Invalid signature"}, status_code=400) # Devuelve 400

    except Exception as e:
        print(f"Webhook Error: Unhandled verification error - {e}")
        return JSONResponse(content={"detail": "Webhook signature verification failed."}, status_code=400)

    event_type = event['type']
    event_data = event['data']
    event_object = event_data['object'] 

    print(f"Received Stripe event: {event_type}")
    print(json.dumps(event_object, indent=2)) 

    return JSONResponse(content={"received": True, "event_type": event_type}, status_code=200)
