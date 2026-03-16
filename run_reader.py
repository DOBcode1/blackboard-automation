import sys
from blackboard.reader import BlackboardReader

def main():
    if len(sys.argv) < 2:
        print("Usage: python run_reader.py <path_to_content_objects.json>")
        print("Example: python run_reader.py output/content_objects_20260316_113920.json")
        sys.exit(1)

    reader = BlackboardReader(sys.argv[1])
    reader.run()

if __name__ == "__main__":
    main()
