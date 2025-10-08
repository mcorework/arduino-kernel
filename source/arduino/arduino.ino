const int pressurePin = A0;  // Analog input
bool isRunning = false;
String inputCommand = "";


void setup() {
  Serial.begin(9600);
  Serial.println("Type 'start' to begin or 'stop' to stop pressure collection.");
}


void loop() {
  // Read serial commands
  if (Serial.available() > 0) {
    char inChar = (char)Serial.read();
    if (inChar == '\n' || inChar == '\r') {
      inputCommand.trim();
      if (inputCommand.equalsIgnoreCase("start")) {
        isRunning = true;
        // Optional: Serial.println("STARTED");
      } else if (inputCommand.equalsIgnoreCase("stop")) {
        isRunning = false;
        // Optional: Serial.println("STOPPED");
      }
      inputCommand = "";
    } else {
      inputCommand += inChar;
    }
  }


  // If collecting data, read sensor and send pressure
  if (isRunning) {
    int sensorValue = analogRead(pressurePin);
    float voltage = sensorValue * (5.0 / 1023.0);


    // === Convert to pressure ===
    float pressure_kPa = (voltage / 5.0 - 0.095) / 0.009;  // Example for MPX4115
    float pressure_bar = pressure_kPa / 100.0;


    Serial.println(pressure_bar, 6);  // Plain number, 6 decimals
    delay(2000);  // ~5 readings per second
  }
}
